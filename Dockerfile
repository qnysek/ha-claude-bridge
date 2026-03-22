FROM python:3.12-alpine

RUN pip install --no-cache-dir fastapi uvicorn anthropic python-dotenv pydantic

WORKDIR /app
COPY server.py .
COPY run.sh .
RUN chmod +x run.sh

CMD ["/app/run.sh"]
