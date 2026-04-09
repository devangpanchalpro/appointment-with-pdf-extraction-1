# Running Medical QA in Docker

This guide explains how to run the Medical Appointment Booking Agent using Docker and Docker Compose.

## Prerequisites

- Docker Desktop installed and running on your machine
- At least 8GB of available memory (Ollama uses significant resources)
- Windows 10/11 or Mac/Linux with Docker installed

## Quick Start

### 1. **Set API Key** (Optional but Recommended)

Create a `.env` file in the project root:

```env
EXTERNAL_API_KEY_DEV=your_actual_api_key_here
OLLAMA_BASE_URL=http://ollama:11434
LLM_MODEL=llama3.2:latest
```

### 2. **Build and Start All Services**

```bash
# Navigate to project directory
cd "d:\desktop proj\prep\New folder (2)"

# Build and start services
docker-compose up -d

# View logs
docker-compose logs -f
```

### 3. **Access the Application**

Once all services are running:

- **Streamlit UI**: http://localhost:8501
- **FastAPI API Docs**: http://localhost:8000/docs
- **FastAPI API**: http://localhost:8000
- **Ollama**: http://localhost:11434

### 4. **First-Time Setup - Pull Ollama Model**

On first run, you need to pull the Llama model:

```bash
# Access the ollama container
docker exec -it medical-qa-ollama ollama pull llama3.2:latest

# Or in one command before starting services
docker-compose up ollama &
docker exec -it medical-qa-ollama ollama pull llama3.2:latest
docker-compose up -d
```

## Common Commands

### View Service Status
```bash
docker-compose ps
```

### View Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f streamlit
docker-compose logs -f fastapi
docker-compose logs -f ollama
```

### Stop Services
```bash
docker-compose down
```

### Stop and Remove Volumes (Clean Start)
```bash
docker-compose down -v
```

### Rebuild Services
```bash
docker-compose build --no-cache
docker-compose up -d
```

### Execute Commands Inside Container
```bash
# Bash into Streamlit container
docker exec -it medical-qa-streamlit bash

# Run Python commands
docker exec medical-qa-streamlit python -c "import streamlit; print(streamlit.__version__)"
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Network                            │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────┐ │
│  │  Streamlit       │  │  FastAPI         │  │  Ollama    │ │
│  │  (Port 8501)     │  │  (Port 8000)     │  │(Port 11434)│ │
│  │                  │  │                  │  │            │ │
│  │  Frontend UI     │  │  Backend API     │  │  LLM Model │ │
│  │                  │  │  + MCP Tools     │  │            │ │
│  └──────────────────┘  └──────────────────┘  └────────────┘ │
│         │                      │                     │        │
│         └──────────────────────┼─────────────────────┘        │
│                                │                              │
│                    Shared user_data volume                    │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | docker | Deployment environment |
| `OLLAMA_BASE_URL` | http://ollama:11434 | Ollama service URL |
| `LLM_MODEL` | llama3.2:latest | LLM model to use |
| `EXTERNAL_API_KEY_DEV` | your_api_key_here | API key for external services |
| `FASTAPI_URL` | http://fastapi:8000 | FastAPI backend URL |

## Troubleshooting

### Services won't start
```bash
# Check Docker daemon is running
docker ps

# Check logs for errors
docker-compose logs --tail=50
```

### Ollama model not found
```bash
# Pull the model manually
docker exec medical-qa-ollama ollama pull llama3.2:latest
```

### Port already in use
```bash
# Find what's using the port (replace 8501 with needed port)
netstat -ano | findstr :8501

# Or change port in docker-compose.yml
# Change "8501:8501" to "8502:8501" for example
```

### Out of memory
- Ensure Docker Desktop has sufficient memory allocation
- Go to Docker Desktop Settings → Resources → Increase memory

### Permission denied errors
```bash
# On Linux/Mac, might need sudo
sudo docker-compose up -d
```

## Development Mode

For development with hot-reload:

```bash
# Modify docker-compose.yml to use --reload flag (already configured)
docker-compose up -d

# Code changes in mounted volumes will hot-reload
```

## Production Deployment

For production, modify `docker-compose.yml`:

1. Remove `--reload` flags
2. Add resource limits
3. Use specific image versions
4. Set proper environment variables
5. Enable persistent logging
6. Add reverse proxy (nginx/traefik)

Example changes:

```yaml
services:
  streamlit:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
```

## Additional Notes

- First startup may take 5-10 minutes as Ollama downloads the model
- Ensure you have at least 10GB free disk space for the Ollama model
- For M1/M2 Macs, use `ollama/ollama:latest` (already ARM-compatible)
- Streamlit and FastAPI containers share the same network for communication

## Need Help?

- Check service status: `docker-compose ps`
- View logs: `docker-compose logs service_name`
- For Docker issues: https://docs.docker.com/
- For Ollama: https://ollama.ai/
- For Streamlit: https://docs.streamlit.io/
