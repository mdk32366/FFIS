#!/bin/bash
# Script to build and push Flat File Scrubber to Docker registry
# Usage: ./build.sh [registry] [tag]
# Example: ./build.sh gcr.io/my-project latest

set -e

REGISTRY=${1:-"flat-file-scrubber"}
TAG=${2:-"latest"}
IMAGE="${REGISTRY}:${TAG}"

echo "Building Docker image: $IMAGE"
docker build -t "$IMAGE" .

echo "✓ Image built successfully!"
echo ""
echo "To push to a registry:"
echo "  docker push $IMAGE"
echo ""
echo "To run locally:"
echo "  docker-compose up -d"
echo ""
echo "To test the image:"
echo "  docker run -p 8501:8501 $IMAGE"
