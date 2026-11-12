"""Ontology lifecycle from the command line.

uv run writer-ontology bootstrap          # seed + publish the core ontology (v1)
uv run writer-ontology draft              # copy the published version into a new draft
uv run writer-ontology publish 2          # validate, sync Neo4j constraints, publish
uv run writer-ontology rollback           # re-publish the previous version
uv run writer-ontology show [N]           # print a version (default: published)
"""

from __future__ import annotations

import argparse
import getpass

from neo4j import GraphDatabase

from writer_service.config import get_settings
from writer_service.governance.db import create_engine_for
from writer_service.governance.uow import UnitOfWork
from writer_service.ontology.neo4j_schema import Neo4jGraphSchema, neo4j_label
from writer_service.ontology.service import OntologyError, OntologyService, spec_of
from writer_service.ontology.spec import OntologySpec


def render(spec: OntologySpec) -> str:
    lines = ["classes:"]
    for c in spec.classes:
        flags = " (deprecated)" if c.deprecated else ""
        parent = f" is-a {c.parent}" if c.parent else ""
        lines.append(f"  :{neo4j_label(c)} key={c.identity_key}{parent}{flags}")
        for p in c.properties:
            lines.append(f"      {p.name}: {p.datatype}{' (required)' if p.required else ''}")
    lines.append("relations:")
    for r in spec.relations:
        flags = " (deprecated)" if r.deprecated else ""
        lines.append(f"  ({r.domain})-[:{r.name}]->({r.range}) {r.cardinality}{flags}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="writer-ontology")
    parser.add_argument("--actor", default=getpass.getuser(), help="recorded in the audit log")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap")
    sub.add_parser("draft")
    sub.add_parser("publish").add_argument("version", type=int)
    sub.add_parser("rollback")
    sub.add_parser("sync", help="re-apply the published version's Neo4j constraints and indexes")
    sub.add_parser("show").add_argument("version", type=int, nargs="?")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = create_engine_for(settings.governance_db_url)

    if args.cmd == "show":
        with UnitOfWork(engine) as uow:
            v = (
                uow.ontology_versions.get_by_number(args.version)
                if args.version is not None
                else uow.ontology_versions.published()
            )
            if v is None:
                raise SystemExit("no such version (run: writer-ontology bootstrap)")
            print(f"v{v.version} [{v.status}]")
            print(render(spec_of(v)))
        return

    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(settings.neo4j_uri, auth=auth) as driver:
        svc = OntologyService(engine, Neo4jGraphSchema(driver))
        try:
            if args.cmd == "bootstrap":
                print(f"published v{svc.bootstrap(args.actor)}")
            elif args.cmd == "draft":
                print(f"created draft v{svc.new_draft(args.actor)}")
            elif args.cmd == "publish":
                svc.publish(args.version, args.actor)
                print(f"published v{args.version}")
            elif args.cmd == "sync":
                r = svc.sync(args.actor)
                created, dropped = ", ".join(r.created) or "-", ", ".join(r.dropped) or "-"
                print(f"created: {created}; dropped: {dropped}")
            else:
                print(f"rolled back to v{svc.rollback(args.actor)}")
        except OntologyError as exc:  # domain errors are user errors: no traceback
            raise SystemExit(f"error: {exc}") from None


if __name__ == "__main__":
    main()
