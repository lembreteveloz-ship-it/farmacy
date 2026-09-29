# Resultado da integração CAF — 29/09/2026

## Importação no banco existente

| Verificação | Resultado |
|---|---|
| Arquivo real lido | `data/catalogo_caf_ipixuna_COMPLETO.json`, 250 itens |
| Simulação | 250 inserções previstas; banco original sem alteração |
| Primeira execução real | 250 inseridos; 0 vinculados a cadastro exato anterior; 0 inválidos/ignorados |
| Segunda execução real | 0 inseridos; 250 já existentes, ignorados corretamente como novas inserções |
| Catálogo após integração | 252 registros: 250 da fonte + 2 anteriores preservados |
| Duplicatas exatas criadas | Nenhuma |
| Correspondências possíveis | 2 ocorrências: Paracetamol e Dexametasona, com forma/apresentação distintas; mantidas separadas |
| Tipos importados | 173 medicamentos, 72 materiais, 5 testes rápidos |
| Categorias preservadas | 9 |
| Controlados | 37; classes preservadas; 2 sem classe na fonte |
| Marcados para revisão | 250, conforme o próprio JSON; não significa falha de importação |
| Integridade SQLite | `integrity_check = ok`; nenhuma violação de chave estrangeira |
| Pedidos criados no banco real pelos testes | Nenhum; todos os cenários operacionais usaram banco temporário |

As 2 ocorrências de possível correspondência não são equivalências confirmadas. Paracetamol corresponde a dois cadastros anteriores com apresentações distintas; Dexametasona corresponde a outra apresentação da própria fonte. O relatório detalha os IDs. Não houve fusão por aproximação.

Backup anterior à primeira importação: `data/backups/farmacia-catalogo-20260929-162640-624509.sqlite3`. A segunda execução também criou backup, sem substituir o primeiro.

Foram comparados registros completos, por SHA-256, antes e depois de cada importação: **2 lotes, 4 movimentações, 1 transferência, 2 eventos de transferência, 0 inventários e 0 requisições**. Todos permaneceram idênticos. Os dois cadastros anteriores conservaram identidade e IDs.

Relatórios detalhados:

- `data/catalog-import-dry-run.json`: simulação e preservação.
- `data/catalog-import-first.json`: inserções, correspondências possíveis, backup e preservação.
- `data/catalog-import-repeat.json`: prova de idempotência no banco real.

## Arquivos criados

- `catalog_orders.py`: migração, validação/importação do catálogo, pesquisa, configuração mensal, regras e persistência de pedidos/recebimentos, snapshots e PDF.
- `scripts/import_catalog.py`: comando explícito com `--dry-run`, organização, backup, transação, rollback e verificação de preservação.
- `public/catalog-orders.js`: Catálogo Mestre, pesquisa na entrada, configuração mensal, planilha, histórico, liberações e recebimentos.
- `public/catalog-orders.css`: tabela desktop, cards em telas pequenas e impressão.
- `data/catalogo_caf_ipixuna_COMPLETO.json`: cópia do arquivo fornecido, sem reconstruir itens.
- `test_catalog_orders.py`: 12 testes de banco e regras.
- `browser_catalog_orders_check.py`: cenário completo em Edge e auditoria axe.
- `catalog-orders-browser-results.json`: resultado de 13 cenários de acessibilidade das novas telas.
- `CATALOGO_E_PEDIDOS.md` e este relatório: operação, decisões, migração e validação.
- Relatórios de importação e backups indicados acima; arquivos `catalog-import-*-output.txt` contêm a saída dos comandos.

## Arquivos alterados

- `import hashlib.py`: integração das rotas, migração, deduplicação manual, metadados e entrada que pode participar da transação do recebimento. A inicialização deixou de imprimir a senha inicial no log.
- `public/app.js`: integração ao renderizador e ações, entrada com pesquisa, metadados no cadastro e continuidade do pedido depois de salvar/receber. A lista Medicamentos filtra somente medicamentos.
- `public/index.html`: carregamento dos novos recursos e atualização de versão dos assets.
- `LEIA-ME.md`: apresentação e documentação do módulo.
- `data/farmacia.sqlite3`: migração e importação descritas acima.
- Os testes existentes regeneraram seu relatório de acessibilidade e imagens de validação, sem mudança de estoque real.

O layout de etiquetas, o renderer de QR e o fluxo de download HTTP existente foram reutilizados, sem substituição.

## Migrações

Oito colunas adicionadas a `medicines`: `item_type`, `volume`, `controlled`, `control_class`, `review_required`, `catalog_source`, `catalog_key`, `original_description`.

Seis tabelas criadas: `catalog_sources`, `monthly_needs`, `orders`, `order_lines`, `order_events`, `order_receipts`, com índices e chaves estrangeiras. Não houve remoção de tabelas/colunas nem substituição dos registros existentes.

A numeração de pedidos usa registro externo `data/order_numbers.sqlite3`, criado na primeira reserva de número. Preserve-o nas migrações de servidor, junto com o registro de códigos de lote. O catálogo não é importado na inicialização.

## Testes e resultados

| Teste | Resultado |
|---|---|
| Suíte completa Python | 64 testes aprovados: 52 existentes + 12 do novo módulo |
| Importar fonte duas vezes / separar tipos, concentrações e controles | Aprovado |
| Vincular correspondência exata anterior sem mudar estoque/histórico | Aprovado |
| Item inválido isolado / ambiguidade de cadastros | Aprovado; erro relatado sem fusão indevida |
| Simulação sem escrita / backup / rollback de falha crítica | Aprovado |
| Cadastro manual duplicado / concentração distinta / revisão seguida de reimportação | Aprovado |
| Necessidade mensal persistida / sugestão não negativa / fotografia imutável | Aprovado |
| Rascunho, quantidade manual, finalização, envio, liberação e cancelamento | Aprovado |
| Recebimentos parcial e total / repetição idempotente / rollback de recebimento | Aprovado |
| Excesso de liberação/recebimento, lote vencido e edição concorrente | Bloqueados corretamente |
| Permissões, unidade, organização, autenticação e CSRF | Verificados; operações indevidas bloqueadas |
| Cenário completo no Edge | Aprovado |
| PDF autenticado, CSV e comando/layout de impressão | Aprovados no navegador; impressão física não executada |
| Cards em 320, 390, 640, 768 e 844 px | Sem rolagem horizontal da página; retrato/paisagem simulados |
| axe nas novas telas | 13 cenários, zero violações detectadas |
| Navegação/acessibilidade existente | Teste `browser_accessibility_check.py` aprovado |
| QR, etiquetas A4 e saída rápida existentes | Teste `browser_lot_check.py` aprovado, incluindo decodificação real do QR |

### Cenário operacional completo

Importou a fonte, repetiu sem duplicar, pesquisou Amoxicilina, escolheu 500 MG, registrou 280 unidades e gerou etiqueta. Salvou necessidade mensal de 1.000, criou pedido e conferiu estoque 280 e sugestão 720. Alterou o pedido para 800, salvou rascunho e finalizou. Liberou 300 e recebeu 200; depois elevou a liberação para 800 e recebeu as 600 restantes. Criou dois lotes/entradas e QR, chegou a estoque 1.080 e status Recebido. A fotografia do estoque do pedido continuou 280. Histórico, PDF, CSV e impressão foram conferidos.

### Ocorrências durante desenvolvimento

- O primeiro teste automatizado tentou iniciar o segundo recebimento antes de terminar a atualização da liberação. A versão do pedido bloqueou a gravação desatualizada; o teste passou a aguardar a resposta e a interface desabilita ações durante o salvamento.
- Um teste de etiquetas não iniciou o Edge no ambiente restrito. Foi repetido com a permissão existente e passou.
- Nenhum erro de dados foi encontrado no JSON fornecido. As revisões marcadas e classes ausentes foram preservadas e não corrigidas por suposição.

## Limites da validação

O teste de impressão verificou o comando e o modo de impressão do navegador, não uma impressora física. As telas móveis foram verificadas em viewports emulados, não em aparelhos físicos. A auditoria automática não comprova conformidade WCAG completa; a validação assistiva com leitor de tela permanece descrita em `ACESSIBILIDADE.md`.

O servidor foi reiniciado e o novo recurso foi verificado por HTTP 200. Atualize a página para carregar Catálogo Mestre e Pedidos.
