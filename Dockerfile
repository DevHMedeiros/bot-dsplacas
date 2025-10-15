# --- Estágio 1: Builder ---
# Usamos uma imagem completa para instalar dependências de forma segura
FROM node:18-alpine AS builder
WORKDIR /app

# Copia apenas os arquivos de manifesto de pacote
COPY package*.json ./

# Instala todas as dependências (incluindo devDependencies)
RUN npm install

# Copia o restante do código-fonte
COPY . .

# --- Estágio 2: Produção ---
# Iniciamos uma nova imagem limpa e muito menor
FROM node:18-alpine
WORKDIR /app

# Cria um usuário e grupo específicos para a aplicação por segurança
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

# Copia apenas as dependências de produção do estágio anterior
COPY --from=builder /app/node_modules ./node_modules
# Copia o código da aplicação do estágio anterior
COPY --from=builder /app ./

# Muda o proprietário dos arquivos para o novo usuário
USER appuser

# Expõe a porta que a aplicação vai escutar
EXPOSE 3000

# Comando para iniciar a aplicação
CMD [ "node", "src/index.js" ]