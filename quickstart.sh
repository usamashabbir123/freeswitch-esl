#!/bin/bash
# FreeSWITCH Logger - Quick Start Script

set -e

echo "=========================================="
echo "FreeSWITCH Logger - Quick Start"
echo "=========================================="
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

echo "✓ Docker found"

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "⚠ docker-compose not found. Trying 'docker compose'..."
    COMPOSE_CMD="docker compose"
else
    COMPOSE_CMD="docker-compose"
fi

echo "✓ Docker Compose ready: $COMPOSE_CMD"
echo ""

# Setup .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env from template..."
    cp .env.template .env
    echo "✓ .env created. Please edit it with your FreeSWITCH connection details:"
    echo "  - ESL_HOST: Your FreeSWITCH hostname or IP"
    echo "  - ESL_PORT: ESL port (usually 8021)"
    echo "  - ESL_PASSWORD: ESL password"
    echo ""
    read -p "Press enter to continue after editing .env..."
else
    echo "✓ .env already exists"
fi

echo ""
echo "🔨 Building Docker image..."
$COMPOSE_CMD build

echo ""
echo "🚀 Starting containers..."
$COMPOSE_CMD up -d

echo ""
echo "✓ Containers started!"
echo ""

echo "📋 Useful commands:"
echo "  View logs:          $COMPOSE_CMD logs -f freeswitch-logger"
echo "  Health check:       $COMPOSE_CMD exec freeswitch-logger python3 /app/healthcheck.py"
echo "  List log files:     $COMPOSE_CMD exec freeswitch-logger ls -lah /var/log/freeswitch-logs/"
echo "  View metrics:       $COMPOSE_CMD logs freeswitch-logger | grep Metrics"
echo "  Stop containers:    $COMPOSE_CMD stop"
echo "  Stop all & cleanup: $COMPOSE_CMD down -v"
echo ""

# Give it a moment to start
sleep 3

echo "📊 Health check:"
$COMPOSE_CMD exec freeswitch-logger python3 /app/healthcheck.py || true

echo ""
echo "=========================================="
echo "✓ Setup complete!"
echo "=========================================="
