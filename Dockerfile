FROM node:22.16.0-alpine

WORKDIR /app
COPY . .

# Create required directories
RUN mkdir -p dist db snapshots
 
# Install git and pnpm
RUN apk add --no-cache git && \
    npm install -g pnpm

# Install dependencies and clean cache
RUN pnpm install && pnpm store prune

# Set permissions for the node user
RUN chown -R node:node /app && \
    chmod -R 755 /app && \
    chmod -R 777 /app/db /app/snapshots

# Switch to non root user
USER node

# Build the application
RUN pnpm run build

CMD ["pnpm", "run", "start"]
