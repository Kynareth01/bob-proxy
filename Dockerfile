FROM python:3.12-slim
WORKDIR /app
COPY server.py .
ENV PORT=8787
EXPOSE 8787
CMD ["python3", "-u", "server.py"]
