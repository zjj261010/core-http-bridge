FROM python:3.10-slim

WORKDIR /app

# Install runtime dependencies
RUN pip install --no-cache-dir \\
    fastapi>=0.115.0 \\
    uvicorn[standard]>=0.30.0 \\
    pydantic>=2.0.0

# Copy bridge code
COPY . /app/

EXPOSE 8080

# Run with uvicorn (NOTE: core-daemon must run separately on the host)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]