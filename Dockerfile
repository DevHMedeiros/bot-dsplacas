# Arquivo: Dockerfile

# --- Estágio de Build ---
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY src/ ./src/

# --- Estágio Final ---
FROM node:18-alpine
WORKDIR /app

# Copia as dependências e o código do estágio de build
COPY --from=builder /app .

EXPOSE 3000
CMD [ "node", "src/index.js" ]