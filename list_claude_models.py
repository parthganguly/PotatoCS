from anthropic import Anthropic

client = Anthropic()
models = client.models.list(limit=20)

for m in models.data:
    print(m.id)
