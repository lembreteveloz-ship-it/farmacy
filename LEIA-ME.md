# Pharmacia

Para instalação, configuração por ambiente, GitHub, Docker, PostgreSQL e pendências de lançamento, consulte o [README de produção](../README.md) e a [auditoria](../AUDITORIA_PRODUCAO.md). O servidor de produção usa `python Pharmacia/serve.py`, executado a partir da pasta acima desta. Não existe mais senha inicial automática: configure a senha da primeira conta no ambiente.

## Menu simplificado e notificações

O menu principal possui tópicos expansíveis: **Início, Estoque, Movimentações e Gestão**. Os itens aparecem abaixo de cada tópico, sem sub-abas no conteúdo. O grupo atual permanece aberto e o item selecionado fica destacado. Inventário, alertas, reposição e etiquetas ficam em Controle de Estoque; saída rápida e perdas ficam em Entradas e Saídas. Catálogo, auditoria e backups estão na área Usuários e Unidades, visível somente para administradores. Outros perfis acessam o catálogo pelo seletor interno de Medicamentos.

O sino no cabeçalho reúne avisos das unidades autorizadas. Seu contador indica eventos ainda pendentes; marcar como lida não resolve o evento nem altera estoque. Veja [NAVEGACAO_E_NOTIFICACOES.md](NAVEGACAO_E_NOTIFICACOES.md) para o mapa completo, funcionamento, permissões e testes.

## Catálogo CAF e Pedidos

O menu agora inclui **Catálogo Mestre** e **Pedidos**, com pesquisa dos itens da CAF, necessidade mensal por unidade, sugestão de pedido, rascunho, liberação e recebimentos parciais vinculados ao estoque. Criar/enviar/liberar um pedido não altera saldo; somente o recebimento físico confirmado gera entrada e QR por lote.

Leia [CATALOGO_E_PEDIDOS.md](CATALOGO_E_PEDIDOS.md) para importação idempotente, simulação, revisão, permissões, migrações, backups e testes. A importação da fonte é explícita e não ocorre a cada inicialização.

## Etiquetas QR em A4 paisagem

As etiquetas por lote usam exclusivamente **QR Code + código interno + nome do medicamento + concentração**. Não são impressos barras lineares, validade, unidade, saldo ou outros campos.

Em **Estoque e lotes → Imprimir etiqueta**, escolha a quantidade total (1 a 1000) e a capacidade da folha:

- 30 etiquetas: 6 colunas × 5 linhas (padrão).
- 35 etiquetas: 7 colunas × 5 linhas.
- 40 etiquetas: 8 colunas × 5 linhas.

A folha é A4 paisagem, 297 × 210 mm, com margens de 5 mm e intervalos de 1 mm. O layout é calculado para maximizar o QR quadrado, incluindo a margem branca de quatro módulos. A prévia e o PDF são renderizados pelo mesmo desenho vetorial. O código é sempre do lote selecionado; não é gerado um identificador novo a cada cópia.

Clique em **Atualizar prévia** após alterar as quantidades. O preenchimento segue da esquerda para a direita e de cima para baixo. A última página conserva a grade: 100 cópias no padrão resultam em 30 + 30 + 30 + 10 etiquetas. A capacidade da folha nunca é recalculada com base no restante.

**Imprimir folhas** imprime a prévia. Escolha A4 paisagem, tamanho real/100% e desative cabeçalhos e rodapés do navegador. **GERAR PDF** baixa as mesmas folhas por URL HTTP autenticada, com tamanho A4 real e preferência de impressão sem escala. Configurações da impressora ou do leitor de PDF podem prevalecer; confira a primeira folha no equipamento real.

O botão Etiqueta no cadastro de medicamentos solicita um lote; sem entrada/lote não há etiqueta. Valores longos são acomodados em linhas menores; se não couberem de forma legível, a geração é recusada, sem cortar silenciosamente o texto.

## Organizações e acessos

O sistema separa os dados por organização: unidades, usuários, catálogo, lotes, movimentações, inventários, transferências, relatórios, auditoria e configurações pertencem a um tenant. O tenant é resolvido pela sessão autenticada; alterar IDs ou cabeçalhos de unidade não concede acesso a outra organização.

Na tela de login, **Cadastrar organização** cria a organização, sua primeira unidade e o primeiro usuário **Administrador da Organização**. Esse administrador gerencia usuários, unidades e dados do próprio tenant. Enfermeiro, Funcionário da Farmácia e Consulta continuam sujeitos às permissões e unidades vinculadas.

O papel global **SuperAdmin** é provisionado pelo ambiente do servidor com `FARMACIA_SUPERADMIN_USERNAME` e `FARMACIA_SUPERADMIN_PASSWORD` configurados juntos; a senha deve ter entre 12 e 256 caracteres. Não grave a senha nos arquivos do projeto. O SuperAdmin escolhe a organização ativa, que fica registrada na sessão. Backups completos e restaurações do banco inteiro ficam restritos a esse papel; administradores organizacionais veem apenas a auditoria do próprio tenant.

Na primeira inicialização após esta atualização, o banco legado é copiado antes da migração. Os registros atuais são associados à **Organização padrão** sem remover unidades, usuários, medicamentos, lotes ou movimentações. Preserve os backups em `data/backups` e faça também cópias em outro dispositivo.

## PDFs por download HTTP

**GERAR PDF** solicita `POST /api/reports/pdf`, protegido por autenticação, CSRF e unidade selecionada. O backend consulta os dados e gera o documento com ReportLab. Retorna uma URL HTTP temporária, válida por 30 minutos e acessível somente ao usuário que gerou o documento enquanto ele tiver acesso à unidade. O download retorna `application/pdf`, `Content-Disposition: attachment` e `Cache-Control: no-store`. Nenhuma janela ou frame é navegado para URLs Blob. O usuário permanece na tela atual; pode abrir o arquivo baixado em seu leitor de PDF antes de imprimir.

Esse fluxo é compartilhado pelas etiquetas de medicamentos/lotes, relatórios semanais e mensais, entradas, saídas, estoque, vencimento nos próximos 90 dias, inventário, transferências, reposição e consolidado da unidade. Na tela Relatórios, **Conteúdo do PDF** seleciona o documento a baixar; a consulta e o CSV existentes continuam mostrando as movimentações. Estoque e inventário são posições atuais, independentemente do período de movimentações.

Não existe módulo de requisições neste projeto. O relatório de reduções de inventário mostra ajustes negativos e suas justificativas; esses registros não são classificados automaticamente como perdas.

Dependências locais do backend em `vendor_python`. Para instalar em outro computador, execute dentro da pasta Pharmacia: `python -m pip install --target vendor_python -r requirements-pdf.txt`. Licenças e metadados estão incluídos nos diretórios das dependências. Referência: https://docs.reportlab.com/reportlab/userguide/ch5_platypus/.

Frames configurados pelo ambiente externo precisam permitir downloads (`allow-downloads` se houver sandbox); o aplicativo não pode remover restrições impostas pelo navegador ou pelo site que o incorpora.

Para iniciar, dê dois cliques em **Iniciar Pharmacia.cmd**, na pasta acima desta. O iniciador abre o navegador e mantém o servidor em segundo plano. Requer Python instalado e disponível no PATH. Endereço padrão: http://127.0.0.1:8000.

## Melhorias

- **Backups** (SuperAdmin): cópia na inicialização e a cada 24 horas enquanto o servidor estiver ligado, além de criação manual. A tela mostra as cópias completas disponíveis. Arquivos em `data/backups`; as cópias são mantidas sem exclusão automática. Copie periodicamente para outro dispositivo para proteger contra falha do computador.
- **Restauração** (SuperAdmin): escolha uma cópia na tela Backups e confirme com sua senha atual. O sistema faz um backup de segurança antes de substituir o banco e encerra todas as sessões. Entre com as credenciais existentes na cópia restaurada. A restauração recupera os dados, não os arquivos do programa.
- **Relatórios**: períodos semanal, mensal e personalizado, filtro por medicamento e exportação CSV compatível com Excel. Datas seguem os registros UTC usados pelo sistema. O botão de exportação consulta os filtros preenchidos. O botão Imprimir permite salvar em PDF pelo navegador.
- **Saídas**: separação automática por validade, com prévia dos lotes e quantidades. Uma saída pode usar vários lotes e gera uma movimentação por lote. Também é possível escolher um lote específico. Lotes vencidos e medicamentos inativos não são utilizados.
- **Reposição**: medicamentos abaixo do mínimo, saldo válido e quantidade faltante. Lotes vencidos não contam como saldo disponível. Inclui exportação CSV e impressão.
- **Histórico cadastral** (administrador): criação, edição e desativação de medicamentos; criação e edição de usuários, incluindo vínculos a unidades. Mostra responsável, data, campos anteriores e novos. Senhas não são armazenadas no histórico. Registros começam nesta atualização; não há reconstrução de alterações antigas.

Os logs do servidor iniciado pelo atalho ficam em `data/server-output.log` e `data/server-error.log`. Fechar o navegador não encerra o servidor.

## Código de barras por lote e Saída Rápida

Após registrar uma entrada, o sistema abre a configuração da folha de etiquetas QR do lote. A etiqueta também pode ser reimpressa em **Estoque e lotes → Imprimir etiqueta**. O QR contém somente o identificador interno `UBS-…`; os dados e o saldo são consultados no banco a cada leitura. Novas entradas no mesmo lote da mesma unidade mantêm seu código. O lote recebido por transferência recebe um código próprio no destino, conservando o vínculo da transferência.

Em **Saída Rápida**, mantenha o foco no campo do código e leia com leitor USB/Bluetooth em modo teclado (HID), preferencialmente com sufixo Enter. O sistema identifica o lote sem pesquisa adicional. Informe quantidade e motivo e confirme. A saída fica restrita àquele lote, mesmo quando há aviso de outro lote com validade mais próxima. O servidor verifica novamente a unidade, medicamento ativo, validade e saldo ao confirmar. Tentativas repetidas da mesma operação não duplicam a baixa.

O botão **ESCANEAR COM A CÂMERA** lê Code 128 e QR Code e solicita permissão ao navegador. Para celular/tablet acessando o servidor pela rede, é necessário servir o sistema por **HTTPS**; uma conexão HTTP ao IP do computador normalmente bloqueia a câmera. O endereço localhost funciona no próprio computador. A câmera é desligada ao sair da tela ou ocultar a aba. Configure acesso à rede e HTTPS antes de usar em outros aparelhos; o iniciador padrão permanece em localhost.

Imprima sem cortar as margens brancas e confira a leitura com o equipamento e a impressora usados na unidade. O saldo não é impresso na etiqueta, pois muda após cada movimentação.

O arquivo `data/lot_codes.sqlite3` mantém o registro permanente dos códigos emitidos, inclusive os emitidos após um backup que posteriormente foi restaurado. **Não apague esse arquivo** e copie-o junto com `farmacia.sqlite3` ao migrar de computador. A restauração pelo sistema preserva esse registro. As bibliotecas de geração e leitura ficam em `public/vendor`, sem consulta a serviços externos para gerar ou ler os códigos.

Para validar os códigos e o fluxo no Edge instalado: `python Pharmacia/browser_lot_check.py` na pasta acima desta. O teste usa banco temporário e não altera o estoque real.

## Cadastro simplificado de medicamentos

O cadastro utiliza nove campos: nome, concentração, forma farmacêutica, apresentação, unidade de estoque, via de administração, estoque mínimo, código de barras do fabricante e observações. Forma, apresentação, unidade e via têm menus com opção Outro/Outra para digitação. Valores anteriores fora das listas são preservados nessa opção. Para uma via combinada, selecione Outra e informe, por exemplo, `Intramuscular / Intravenosa`.

Essas informações identificam o medicamento nas movimentações, estoque, inventário, transferências e relatórios. Os CSVs incluem forma, apresentação, via e código do fabricante. No cadastro, Etiqueta abre uma versão para impressão com os nove campos, incluindo o código do fabricante em texto. Em entrada e saída, o campo de localização aceita leitores que digitam o código e enviam Enter. Códigos duplicados exigem seleção pelo nome para evitar escolher o medicamento errado. A apresentação não converte quantidades: o saldo é sempre contado na unidade de estoque escolhida.

## Transferências entre unidades

Em **Transferências → Nova transferência**, selecione origem, destino, medicamento e lote. A validade vem do lote e não pode ser alterada. Informe quantidade, responsável e observações. A origem precisa ser uma unidade à qual o usuário tem acesso; o destino pode ser qualquer unidade da mesma organização.

O envio reduz imediatamente o saldo da origem. O destino permanece inalterado até a confirmação. Na unidade de destino, o dashboard e o sino mostram as transferências pendentes, com atualização a cada 20 segundos enquanto a aba está visível. O recebimento conserva lote e validade e registra usuário, horário e movimentação. Confirmações repetidas são bloqueadas.

- Administrador, enfermeiro e funcionário da farmácia podem enviar, receber, informar divergência ou recusar, respeitando seu acesso à unidade. Consulta apenas visualiza.
- A divergência exige quantidade física, justificativa e permite observações. Fica com status **Divergência**, sem entrada no destino. Um administrador ou enfermeiro do destino pode resolver e confirmar a entrada da quantidade informada. A diferença permanece registrada, sem reposição automática na origem.
- A recusa exige justificativa e não devolve automaticamente o estoque à origem. Administrador ou enfermeiro da origem pode estornar após conferir a devolução física de toda a quantidade enviada.
- O cancelamento é permitido pela origem somente enquanto pendente, com justificativa e conferência física. Devolve a quantidade à origem.
- As seis situações são Pendente de Recebimento, Recebida, Divergência, Recusada, Cancelada e Estornada. O histórico permanece disponível em todas elas; o módulo não oferece exclusão.
- Lotes vencidos ou com validade conflitante no destino não podem ser recebidos. Use a recusa justificada e o estorno após devolução.

### Aviso por e-mail

Para ativar, configure no ambiente do servidor `FARMACIA_SMTP_HOST` e `FARMACIA_SMTP_FROM`. As configurações adicionais são `FARMACIA_SMTP_PORT` (padrão 587), `FARMACIA_SMTP_USER`, `FARMACIA_SMTP_PASSWORD` e `FARMACIA_SMTP_TLS` (padrão 1, STARTTLS). Reinicie o servidor depois de configurar. Não coloque senhas nos arquivos do projeto.

Os avisos são enviados aos usuários ativos da organização com e-mail cadastrado e perfil de enfermeiro ou funcionário da farmácia vinculados ao destino, além dos administradores organizacionais ativos com e-mail. Falhas no e-mail não desfazem a transferência; a notificação interna continua disponível. Nenhuma conta de e-mail foi configurada automaticamente.

Para executar os testes, na pasta acima desta: `python -m unittest discover -s Pharmacia -p "test_*.py" -v`.

## Senhas e acessibilidade

Os campos de senha possuem botão de olho com suporte a teclado e indicação acessível do estado. Isso revela apenas o texto digitado no campo; senhas cadastradas continuam protegidas por hash e não são consultáveis pelo administrador.

Foram adicionados foco visível, ajustes de contraste e texto, áreas de toque maiores, identificação de campos obrigatórios e erros, navegação dos modais, tabelas roláveis pelo teclado e mensagens textuais para leitura de QR. Antes da expiração, o sistema avisa e permite continuar conectado sem limpar o formulário.

Consulte [ACESSIBILIDADE.md](ACESSIBILIDADE.md) para os testes reproduzíveis, resultados e verificações manuais ainda necessárias com leitor de tela e dispositivos reais. A auditoria automática não equivale a certificação WCAG.
