# Navegação e Central de Notificações

## Mapa da navegação anterior para a nova

O levantamento foi feito antes de reorganizar o menu. Os identificadores das telas e handlers anteriores foram mantidos. Os quatro tópicos expandem os destinos diretamente no menu, com setas e `aria-expanded`. O tópico da página atual permanece aberto; os demais podem ser recolhidos. O item atual usa destaque e `aria-current="page"`. Não há sub-abas na área de conteúdo.

| Função anterior | Local atual |
|---|---|
| Dashboard | Início → Visão Geral |
| Medicamentos | Estoque → Medicamentos |
| Estoque, lotes, etiquetas e QR | Estoque → Controle de Estoque, visualização padrão |
| Inventário e ajustes por contagem | Controle de Estoque → seletor Inventário e conferência |
| Alertas | Controle de Estoque → seletor Alertas de estoque e validade |
| Reposição e posição de estoque | Controle de Estoque → seletor Posição e necessidade de reposição |
| Entradas, saídas e histórico | Movimentações → Entradas e Saídas |
| Saída rápida por leitura | Entradas e Saídas → seletor Saída rápida por QR |
| Perdas | Entradas e Saídas → seletor Perdas; botão Registrar perda conforme perfil |
| Histórico de ajustes | Entradas e Saídas → seletor Histórico de ajustes |
| Transferências | Movimentações → Transferências |
| Pedidos à CAF e necessidades mensais | Gestão → Pedidos |
| Requisições entre unidades | Botão dentro de Pedidos |
| Relatórios | Gestão → Relatórios |
| Usuários, unidades e permissões | Gestão → Usuários e Unidades |
| Catálogo Mestre | Usuários e Unidades → seletor Catálogo Mestre |
| Auditoria | Usuários e Unidades → seletor Auditoria administrativa, para administradores |
| Backup e restauração | Usuários e Unidades → seletor Backups e restauração, somente SuperAdmin |
| Perfil, foto, senha, organização/unidade e saída da sessão | Cabeçalho, preservados |

As visualizações internas usam seletores, sem novos níveis de sub-abas. Perfis sem administração não veem Usuários e Unidades; continuam acessando o catálogo pelo seletor interno de Medicamentos, sem receber acesso à API de usuários, auditoria ou backups. As regras existentes do backend permanecem aplicadas. No celular, os mesmos tópicos aparecem em lista compacta, com botões de pelo menos 44 pixels e foco visível.

## Sino e painel

O cabeçalho permanece visível durante a rolagem. O sino fica à direita, com contador de **eventos ainda pendentes**, inclusive os já lidos. Sem pendências, mostra somente o sino. O painel informa separadamente os não lidos e permite filtrar pendentes, não lidos ou todos, incluindo resolvidos recentes.

A leitura é individual por usuário e persiste no banco. Marcar uma ou todas como lidas não confirma recebimento, não altera saldo e não resolve a condição. O evento fica resolvido quando a condição deixa de existir: transferência recebida/cancelada/estornada, regularização de estoque ou retirada do saldo do lote vencido, por exemplo. Uma ocorrência posterior produz novo aviso. Alteração relevante do saldo torna o aviso não lido novamente.

Notificações são atualizadas a cada 30 segundos enquanto a aba está visível, ao voltar à aba e após operações existentes. Não há consultas simultâneas da mesma atualização. Novos avisos geram faixa no topo por 12 segundos, permanecendo no painel depois. Críticos também têm região de anúncio assertivo; todos usam texto e indicação de nível, além da cor.

Prioridade: divergências/recusas críticas, transferências pendentes, vencidos, próximos do vencimento, estoque zerado e estoque mínimo atingido. Dentro de cada prioridade, eventos mais recentes vêm primeiro.

Cada aviso mostra título, descrição, unidade, data/hora, situação da leitura, situação da pendência, status do evento e ação direta. Vencimentos usam a data de detecção, explicitamente identificada. A lista exibe 30 avisos por vez, com botão Mostrar mais. Resolvidos dos últimos 30 dias podem ser consultados no filtro Todas; os registros não são apagados por essa filtragem.

Os avisos abrangem as unidades **atualmente autorizadas** da organização: funcionários veem seus vínculos; administradores veem as unidades da organização selecionada. O clique troca para a unidade do aviso, abre a área correta e destaca o lote/transferência ou a posição atual do item. Uma unidade de outra organização não pode ser acessada por alteração de IDs.

O estoque mínimo inclui o limiar exato (saldo igual ao mínimo). Estoque zerado é avisado para itens com mínimo configurado, necessidade mensal positiva ou histórico de lote na unidade. Itens que apenas existem no Catálogo Mestre, sem vínculo operacional com aquela unidade, não geram centenas de avisos de estoque zerado.

## Implementação

- `public/navigation-notifications.js`: grupos, áreas, seletores, sino, painel, leitura, links diretos, polling e anúncios.
- `public/navigation-notifications.css`: cabeçalho fixo durante rolagem, menu compacto, painel lateral, celular, foco e impressão.
- `notifications.py`: geração/deduplicação, resolução, prioridades, isolamento e leitura individual.
- `import hashlib.py`: migração aditiva e rotas novas `GET /api/notifications` e `POST /api/notifications/read`. O POST exige sessão e CSRF.
- `public/app.js` e `public/index.html`: integração. A rota anterior de notificações de transferências permanece disponível; o botão e os cards duplicados foram substituídos pelo sino central.

Migração: tabelas `notifications` e `notification_reads`, com índices de escopo e unicidade de evento ativo. Nenhuma tabela de estoque, pedido ou movimentação foi removida. Backup pré-migração: `data/backups/farmacia-20260929-204313-771117.sqlite3`.

O login ganhou fallback explícito POST, para evitar que o navegador coloque campos de senha na URL caso JavaScript não carregue. Autenticação e hashes permanecem iguais.

## Validação

Na atualização dos tópicos expansíveis, foram reexecutados os testes de navegação/notificações e acessibilidade: 8 e 34 cenários axe, respectivamente, sem violações detectadas. A navegação verifica expansão por Enter, recolhimento por Espaço, manutenção do foco, grupo ativo aberto, ausência de sub-abas e ocultação da administração para o funcionário. Esta alteração de menu não requer migração de banco.

- 64 testes existentes de backend aprovados.
- 7 testes novos em `test_notifications.py`: autorização/unidades/organização, leitura individual, estoque imutável ao ler, persistência, recorrência, resolução, prioridades, divergência/recusa e prevenção de avisos de catálogo sem vínculo operacional.
- `browser_accessibility_check.py`: navegação por teclado e fluxos existentes aprovados, usando os novos grupos e seletores.
- `browser_catalog_orders_check.py`: cenário completo de importação, pedidos, recebimentos, PDF, CSV e QR aprovado.
- `browser_lot_check.py`: geração/decodificação de QR, etiquetas A4, download e saída rápida aprovados.
- `browser_navigation_check.py`: destinos do menu, quatro grupos, teclado/Esc/foco, contador, leitura individual/em lote persistente, polling real, links diretos, administrador e funcionário com unidades distintas, CSRF, painel desktop/celular e axe.

O resultado axe do painel fica em `navigation-notifications-test-results.json`. As telas foram verificadas em larguras de 320, 390, 640, 844 e 1440 px. Isso representa emulação no navegador; não é certificação de uso com leitor de tela ou aparelhos físicos.

Durante o desenvolvimento, o teste novo foi corrigido para utilizar a mesma instância de banco temporário do seu fixture. Uma tentativa de login de teste atingiu o contador do administrador no banco local; o incremento causado pelo teste foi revertido. Não houve alteração de senha, estoque ou autorização.
