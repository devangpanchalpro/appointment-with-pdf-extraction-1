# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies (required for pdf2image and pytesseract)
RUN apt-get update && apt-get install -y \
    poppler-utils \
    tesseract-ocr \
    libopenblas-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --upgrade pip --default-timeout=1000 --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org && \
    pip install -r requirements.txt --default-timeout=1000 --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org

# Copy project files
COPY . .

# Install the package in development mode
RUN pip install -e .

# Expose ports for Streamlit (8501) and FastAPI (8000)
EXPOSE 8501 8000

# Default command runs Streamlit (can be overridden for FastAPI)
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
