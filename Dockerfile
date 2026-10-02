# ---- Referral pipeline image ----
# Official slim Python image (pandas 3.x needs Python >= 3.11)
FROM python:3.11-slim

# Don't write .pyc files; flush logs straight to the console
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data/raw \
    OUTPUT_DIR=/app/output \
    DEFAULT_TZ=Asia/Jakarta

# Working directory inside the container
WORKDIR /app

# Install dependencies first so Docker can cache this layer between code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and the sample source data
# (mount your own data over /app/data/raw at run time to use different files)
COPY src/ ./src/
COPY data/ ./data/

# Folder that is mounted from the host so reports survive after the container exits
RUN mkdir -p /app/output
VOLUME ["/app/output"]

# Run profiling first, then the referral pipeline; stop if either step fails
CMD ["sh", "-c", "python src/data_profiling.py && python src/referral_pipeline.py"]
