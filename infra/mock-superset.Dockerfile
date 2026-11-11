FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.32" "pydantic>=2.9"
COPY src/demo_env ./demo_env
USER nobody
EXPOSE 8088
CMD ["uvicorn", "demo_env.mock_superset:create_mock_superset", "--factory", "--host", "0.0.0.0", "--port", "8088"]
