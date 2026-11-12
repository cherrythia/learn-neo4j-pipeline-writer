import { GlobalRegistrator } from "@happy-dom/global-registrator";

GlobalRegistrator.register();

// happy-dom has no document.fonts, which Mantine's autosize JsonInput listens to.
const doc = document as unknown as { fonts?: unknown };
doc.fonts ??= { addEventListener() {}, removeEventListener() {} };
