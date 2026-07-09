# Monitor Smiles — atalhos de execução.
# Rode `make` ou `make ajuda` para ver os comandos disponíveis.

SHELL := /bin/bash
ENV_FILE := $(HOME)/.smiles-monitor.env
PLIST := com.gustavo.smilesmonitor.plist
PLIST_DEST := $(HOME)/Library/LaunchAgents/$(PLIST)

.DEFAULT_GOAL := ajuda

.PHONY: ajuda instalar env testar rodar agendar desagendar status log limpar

ajuda:  ## Mostra esta ajuda
	@echo "Monitor Smiles — comandos:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-12s %s\n", $$1, $$2}'
	@echo ""

instalar:  ## Instala dependências (Python + Chromium do Playwright)
	python3 -m pip install -r requirements.txt --break-system-packages
	python3 -m playwright install chromium
	chmod +x rodar_local.sh
	@echo "✓ Dependências instaladas."

env:  ## Cria o arquivo de credenciais do e-mail (se ainda não existir)
	@if [ -f "$(ENV_FILE)" ]; then \
		echo "✓ $(ENV_FILE) já existe (não vou sobrescrever)."; \
	else \
		printf 'export RESEND_API_KEY="re_sua_chave_aqui"\nexport MAIL_FROM="noreply@seudominio.com.br"\nexport MAIL_TO="voce@gmail.com"\n' > "$(ENV_FILE)"; \
		echo "✓ Criado $(ENV_FILE) — edite com suas credenciais do Resend."; \
	fi

testar:  ## Roda uma busca rápida (3 destinos, 2 datas) sem publicar
	@set -a; [ -f "$(ENV_FILE)" ] && source "$(ENV_FILE)"; set +a; \
		python3 buscar.py --max-destinos 3 --max-datas 2

rodar:  ## Faz a busca completa e publica no GitHub (busca + push + e-mail)
	./rodar_local.sh

agendar:  ## Ativa o agendamento automático (launchd, 4x ao dia)
	cp $(PLIST) "$(HOME)/Library/LaunchAgents/"
	launchctl load "$(PLIST_DEST)"
	@echo "✓ Agendado. Roda às 8h, 12h, 16h e 20h (com o Mac ligado)."

desagendar:  ## Desliga o agendamento automático
	launchctl unload "$(PLIST_DEST)" 2>/dev/null || true
	@echo "✓ Agendamento desativado."

status:  ## Mostra se o agendamento está ativo
	@if launchctl list | grep -q smilesmonitor; then \
		echo "✓ Agendamento ATIVO."; \
	else \
		echo "✗ Agendamento inativo. Use 'make agendar'."; \
	fi

log:  ## Mostra as últimas linhas do log da execução
	@tail -n 40 monitor.log 2>/dev/null || echo "Sem log ainda (rode 'make rodar' primeiro)."

limpar:  ## Remove arquivos temporários (cache, log, alertas)
	rm -rf __pycache__ monitor.log alertas.md
	@echo "✓ Limpo."
