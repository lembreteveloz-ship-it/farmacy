# Catálogo CAF e pedidos mensais

## Organização dos dados

O cadastro `medicines` continua sendo o cadastro mestre da organização, sem representar saldo físico. Foi estendido para medicamentos, materiais e testes rápidos. Os saldos continuam exclusivamente em `lots`, separados por unidade. A tela Medicamentos mantém apenas medicamentos; o Catálogo Mestre permite filtrar os três tipos e as categorias.

Cada item preserva tipo, categoria, volume, classificação de controle, indicação de revisão, origem, chave e descrição original. `catalog_sources` guarda também o objeto JSON original integral, inclusive página da fonte. Alterações administrativas não reescrevem essa cópia da fonte nem as fotografias dos pedidos.

O JSON fornecido contém 250 itens: 173 medicamentos, 72 materiais e 5 testes rápidos; nove categorias; 37 controlados. Todos os 250 vêm com `review_required=true`. Dois controlados não possuem classe informada na fonte. Nenhuma classe foi inventada. A revisão está disponível ao administrador em Catálogo Mestre → Revisar item.

Quando a fonte não informa unidade de estoque, o cadastro usa a forma Comprimido/Cápsula quando explícita; nos demais casos usa Unidade. Confira a unidade antes da primeira entrada. A apresentação não converte caixas, frascos ou comprimidos automaticamente. Itens com movimentação conservam a proteção existente contra troca de unidade de estoque.

## Importação explícita

Na pasta principal do projeto:

```powershell
python Pharmacia/scripts/import_catalog.py --dry-run --report Pharmacia/data/catalog-import-dry-run.json
python Pharmacia/scripts/import_catalog.py --report Pharmacia/data/catalog-import-first.json
```

O caminho padrão é `Pharmacia/data/catalogo_caf_ipixuna_COMPLETO.json`. É possível informar `--source`, `--database` e `--organization-id`. Se houver mais de uma organização ativa, a organização precisa ser escolhida explicitamente. O importador não é chamado na inicialização do servidor.

O modo de simulação trabalha numa cópia em memória e não altera o banco original. A execução real cria backup exclusivo em `data/backups/farmacia-catalogo-*.sqlite3`, antes da migração/importação. Cada execução produz resumo, ações, itens inválidos, possíveis correspondências e verificações de preservação.

Identificação: origem + `catalog_key`, seguida de nome + concentração + forma + apresentação, normalizados para caixa, acentos e espaços, dentro do mesmo tipo e organização. Uma correspondência exata única vincula metadados sem alterar identidade, saldo, lotes ou histórico. Correspondências ambíguas são relatadas para revisão. Mesmo nome/concentração com forma ou apresentação diferente permanece separado e aparece como possível correspondência no relatório.

Registros inválidos isolados são relatados e não impedem os válidos. Falha crítica de banco desfaz toda a transação. Há comparação integral por SHA-256 de lotes, movimentações, inventários, transferências, eventos de transferência e requisições antes e depois. Nenhum ID anterior de medicamento pode desaparecer. Uma nova importação não desfaz revisões manuais.

## Uso

1. Catálogo Mestre → pesquise nome, concentração, forma, apresentação ou descrição da fonte; filtros de tipo, categoria e revisão.
2. Registrar entrada → pesquise, selecione a apresentação, confira a unidade de estoque e informe lote, validade, quantidade e dados do recebimento. O sistema mantém códigos, QR, FEFO e histórico existentes.
3. Pedidos → Necessidade Mensal → preencha várias linhas e salve. A configuração pertence à unidade ativa. Administrador e enfermeiro podem configurá-la.
4. Novo Pedido → escolha unidade autorizada, mês, ano, responsável e tipo. “Todos” exige perfil de administrador ou enfermeiro.
5. O pedido grava nome da unidade, identificação de cada item, necessidade mensal e **estoque físico no momento da criação**, inclusive saldo de lotes vencidos. Sugestão = máximo entre necessidade menos estoque e zero. Não é atualizada retroativamente.
6. Edite Quantidade a Pedir na planilha, com Tab entre os campos. Filtre por nome, categoria, reposição, estoque zerado ou oculte sugestão zero. Filtros não apagam valores das outras linhas.
7. Salve o rascunho ou finalize. Depois de finalizado, a quantidade solicitada fica bloqueada. “Marcar como Enviado” registra o estado; não envia e-mail nem altera estoque.
8. Administrador/enfermeiro informa a quantidade liberada pela CAF, limitada ao solicitado e nunca abaixo do que já foi recebido. A liberação também não altera estoque.
9. Confirme cada recebimento físico com quantidade, lote, validade, fabricação opcional, documento e observações. Uma única transação cria entrada, movimentação, vínculo ao pedido e atualização do recebido. O QR usa exatamente o mecanismo e o layout anteriores.

Liberação inferior ao solicitado ou recebimento incompleto mantém “Parcialmente atendido”. É possível aumentar posteriormente a liberação e registrar outros recebimentos. “Recebido” exige receber toda a quantidade solicitada. Cancelar exige justificativa e não desfaz entradas já realizadas. Pedidos e seus eventos não têm exclusão definitiva.

Permissões: Consulta só visualiza/exporta; funcionários da farmácia podem criar pedidos de um tipo, salvar/finalizar/enviar e receber; administrador/enfermeiro configuram necessidades, registram liberações, cancelam e podem pedir Todos. Todo acesso exige a organização da sessão e uma unidade autorizada. Alterações POST exigem CSRF.

## Histórico e exportação

O Histórico de Pedidos filtra a unidade ativa, mês, ano, status, item e responsável. A ficha conserva a fotografia do pedido, eventos e todos os recebimentos com lote, documento e etiqueta QR. Necessidades e estoque posteriores não reescrevem os dados anteriores.

PDF usa o backend e o download HTTP autenticado existente. CSV abre no Excel; há impressão da ficha. Salve as alterações antes de exportar, imprimir ou receber. Em celular a planilha muda para cards; os campos mantêm nomes acessíveis, teclado e foco visível.

## Migração e numeração

Campos adicionados a `medicines`: `item_type`, `volume`, `controlled`, `control_class`, `review_required`, `catalog_source`, `catalog_key`, `original_description`.

Novas tabelas: `catalog_sources`, `monthly_needs`, `orders`, `order_lines`, `order_events`, `order_receipts`. A migração cria estrutura, não importa o catálogo. Os dados antigos não são removidos.

O registro `data/order_numbers.sqlite3` mantém números `PED-ano-sequência` fora do banco restaurável, como já ocorre com códigos de lote. **Preserve esse arquivo e copie-o junto do banco e de `lot_codes.sqlite3` ao migrar o servidor.** Números reservados não são reutilizados, mesmo após falha ou restauração. Lacunas na sequência são normais.

Versões impedem que dois usuários sobrescrevam silenciosamente o mesmo pedido. Recebimentos usam identificador único: repetir a mesma confirmação não duplica estoque; mudar dados usando o identificador anterior é recusado.

## Verificação

```powershell
python -m unittest discover -s Pharmacia -p "test_*.py"
python Pharmacia/browser_catalog_orders_check.py
python Pharmacia/browser_accessibility_check.py
python Pharmacia/browser_lot_check.py
```

Os testes usam banco temporário. O teste de navegador importa a fonte duas vezes e executa pesquisa, entrada, necessidade, pedido, rascunho, finalização, liberações, dois recebimentos, QR, saldo, histórico, PDF, CSV e impressão simulada. Também confere cards em cinco tamanhos e roda axe. Não substitui teste manual em impressora, câmera ou leitor de tela físico; veja `ACESSIBILIDADE.md`.

Resultados da importação real e verificação de preservação ficam nos relatórios JSON em `data/catalog-import-*.json`. A auditoria do novo navegador fica em `catalog-orders-browser-results.json`.
