# Deploying Flat File Scrubber with Docker

This guide covers building, running, and deploying the Flat File Scrubber application using Docker.

## Prerequisites

- Docker Desktop (Windows/macOS) or Docker Engine (Linux)
- Docker Compose (included with Docker Desktop)
- Git (for cloning the repository)

## Quick Start — Local Development

### 1. Build the Docker Image

```bash
cd FFIS
docker build -t flat-file-scrubber:latest .
```

### 2. Run with Docker Compose

```bash
# Create your .env file from the example
cp .dockerenv.example .env

# Edit .env with your configuration
# nano .env  (or use your preferred editor)

# Start the application
docker-compose up -d
```

The application will be available at `http://localhost:8501`

### 3. View Logs

```bash
docker-compose logs -f flat-file-scrubber
```

### 4. Stop the Application

```bash
docker-compose down
```

---

## Configuration for Snowflake Deployment

### Set Environment Variables

Update your `.env` file or pass environment variables to the container:

```bash
export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
export SNOWFLAKE_USER=your_username
export SNOWFLAKE_PASSWORD=your_password
export SNOWFLAKE_DATABASE=your_database
export SNOWFLAKE_SCHEMA=your_schema
export SNOWFLAKE_WAREHOUSE=your_warehouse
```

### Mount Configuration Files

Place your `secrets.json` in the project root. The docker-compose.yml will automatically mount it as read-only:

```json
{
  "snowflake": {
    "account": "xy12345.us-east-1",
    "user": "your_username",
    "password": "your_password",
    "database": "your_database",
    "schema": "your_schema",
    "warehouse": "your_warehouse"
  }
}
```

---

## Production Deployment Options

### Option 1: Cloud Run (Google Cloud)

```bash
# Build and push to Google Cloud Registry
gcloud builds submit --tag gcr.io/PROJECT_ID/flat-file-scrubber:latest

# Deploy to Cloud Run
gcloud run deploy flat-file-scrubber \
  --image gcr.io/PROJECT_ID/flat-file-scrubber:latest \
  --platform managed \
  --port 8501 \
  --memory 2Gi \
  --cpu 2 \
  --set-env-vars SNOWFLAKE_ACCOUNT=xy12345.us-east-1,... \
  --allow-unauthenticated
```

### Option 2: AWS ECS (Elastic Container Service)

```bash
# Build and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com

docker build -t flat-file-scrubber:latest .
docker tag flat-file-scrubber:latest 123456789.dkr.ecr.us-east-1.amazonaws.com/flat-file-scrubber:latest
docker push 123456789.dkr.ecr.us-east-1.amazonaws.com/flat-file-scrubber:latest
```

Then create an ECS task definition and service pointing to the ECR image.

### Option 3: Kubernetes (K8s)

Create a `deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flat-file-scrubber
spec:
  replicas: 2
  selector:
    matchLabels:
      app: flat-file-scrubber
  template:
    metadata:
      labels:
        app: flat-file-scrubber
    spec:
      containers:
      - name: app
        image: flat-file-scrubber:latest
        ports:
        - containerPort: 8501
        env:
        - name: SNOWFLAKE_ACCOUNT
          valueFrom:
            secretKeyRef:
              name: snowflake-secrets
              key: account
        - name: SNOWFLAKE_USER
          valueFrom:
            secretKeyRef:
              name: snowflake-secrets
              key: user
        - name: SNOWFLAKE_PASSWORD
          valueFrom:
            secretKeyRef:
              name: snowflake-secrets
              key: password
        resources:
          requests:
            memory: "1Gi"
            cpu: "1"
          limits:
            memory: "2Gi"
            cpu: "2"
        livenessProbe:
          httpGet:
            path: /_stcore/health
            port: 8501
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /_stcore/health
            port: 8501
          initialDelaySeconds: 5
          periodSeconds: 10
```

Deploy with:
```bash
kubectl apply -f deployment.yaml
```

### Option 4: Docker Swarm

```bash
docker swarm init
docker stack deploy -c docker-compose.yml ffis
```

---

## Security Best Practices

### 1. **Secrets Management**
- Never commit `secrets.json` or `.env` to version control
- Use Docker secrets in production (Swarm) or managed secrets (Cloud Run, ECS)
- Rotate credentials regularly

### 2. **Image Security**
```bash
# Scan your image for vulnerabilities
docker scan flat-file-scrubber:latest

# Use a private Docker registry
docker tag flat-file-scrubber:latest myregistry.azurecr.io/flat-file-scrubber:latest
docker push myregistry.azurecr.io/flat-file-scrubber:latest
```

### 3. **Network Security**
- Run behind a reverse proxy (nginx, Traefik) with TLS/SSL
- Limit container resource access
- Use network policies in Kubernetes

### 4. **Runtime Security**
- Don't run as root in production
- Use read-only file systems where possible
- Enable container logging and monitoring

---

## Monitoring and Logging

### Local Logs
```bash
docker-compose logs flat-file-scrubber
docker-compose logs -f --tail 100 flat-file-scrubber
```

### Cloud Logging
- **Google Cloud**: Automatically sends to Cloud Logging
- **AWS**: Configure container logging in ECS task definition
- **Kubernetes**: Use kubectl logs or a logging solution (ELK, Datadog)

---

## Troubleshooting

### Container won't start
```bash
# Check logs
docker-compose logs flat-file-scrubber

# Test building locally
docker build --progress=plain -t flat-file-scrubber:latest .
```

### Port already in use
```bash
# Change port in docker-compose.yml
# Or kill existing container:
docker-compose down
docker ps -a  # Check for zombie containers
docker rm <container_id>
```

### Permission denied on mounted volumes
```bash
# Ensure proper file permissions
chmod 644 secrets.json
chmod 644 .env
```

### Out of memory errors
Increase memory in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      memory: 4G
```

---

## Updating the Application

```bash
# Pull latest code
git pull origin main

# Rebuild image
docker build -t flat-file-scrubber:latest .

# Restart container
docker-compose up -d --force-recreate
```

---

## Resources

- [Docker Documentation](https://docs.docker.com/)
- [Streamlit Deployment Guide](https://docs.streamlit.io/streamlit-community-cloud/deploy-your-app)
- [Snowflake Python Connector](https://docs.snowflake.com/en/developer-guide/python-connector/)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
