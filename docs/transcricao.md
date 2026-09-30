# Transcrição automática de áudios

Esta branch adiciona transcrição automática ao bot de feedback dos TRs.

## Comportamento

Ao receber uma mensagem de voz (`voice`) ou um arquivo de áudio (`audio`), o bot:

1. baixa o áudio temporariamente;
2. salva o áudio original em `FeedbackTRS/Ciclo-NN/Ciclo-NN-Telegram/`;
3. transcreve localmente com `faster-whisper` em português;
4. salva a transcrição em um arquivo `.txt` na mesma pasta;
5. registra os dois nomes no `_chat.txt`;
6. responde no Telegram com a transcrição.

## Modelo

O padrão é `large-v3-turbo`, escolhido como o melhor modelo viável no VPS atual (1,9 GiB de RAM):

```text
WHISPER_MODEL=large-v3-turbo
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=auto
```

O `large-v3` continua disponível e tem qualidade máxima, mas requer uma máquina com mais memória. Para usá-lo explicitamente:

```text
WHISPER_MODEL=large-v3
```

No VPS sem CUDA, o código faz fallback para `cpu` + `int8`. O download do modelo ocorre no primeiro áudio processado e pode consumir vários GB de RAM durante a execução. Para uma operação mais leve, pode-se usar explicitamente:

```text
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

## Instalação

No ambiente virtual do bot:

```bash
pip install -r requirements.txt
```

A branch não deve ser iniciada junto com a branch principal usando o mesmo token do Telegram: isso criaria dois consumidores de polling. Para testar, pare o serviço atual, confira o status, troque para esta branch e só então inicie o serviço.

## Configuração do serviço

As variáveis podem ser adicionadas ao unit file ou a um EnvironmentFile fora do Git:

```ini
Environment=WHISPER_MODEL=large-v3
Environment=WHISPER_DEVICE=auto
Environment=WHISPER_COMPUTE_TYPE=auto
```

O `token.json` do Google Drive e o token do Telegram não devem entrar no repositório.
