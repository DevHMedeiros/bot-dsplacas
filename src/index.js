// Arquivo: src/index.js

const express = require('express');
const fs = require('fs');
const app = express();
const port = 3000;

// Função para ler um Docker Secret de forma segura
function getSecret(secretName) {
  try {
    // Docker monta os secrets como arquivos neste caminho
    return fs.readFileSync(`/run/secrets/${secretName}`, 'utf8').trim();
  } catch (err) {
    // Se o secret não existir, retorna undefined ou um valor padrão
    console.warn(`Secret ${secretName} não encontrado.`);
    return undefined;
  }
}

// Carrega os segredos
const dbPassword = getSecret('db_password'); // O nome aqui será definido no docker-compose.yml
const apiKey = getSecret('api_key');

app.get('/', (req, res) => {
  if (dbPassword && apiKey) {
    res.send(`Aplicação rodando! A chave de API começa com: ${apiKey.substring(0, 3)}...`);
    // Aqui você usaria a variável 'dbPassword' para conectar ao banco
  } else {
    res.status(500).send('Erro: Segredos não foram carregados corretamente.');
  }
});

app.listen(port, () => {
  console.log(`App ouvindo na porta ${port}`);
});