# Acessibilidade

Referência: [WCAG 2.2, critérios A e AA do W3C](https://www.w3.org/WAI/WCAG22/quickref/).
Este documento registra implementação e verificação; não constitui certificação de conformidade.

## Comportamentos implementados

- Todos os campos de senha recebem um botão de olho, operável com Tab, Enter e Espaço. O nome alterna entre “Mostrar senha” e “Ocultar senha”; `aria-controls` associa o botão ao campo e `aria-pressed` informa o estado. A senha inicia oculta e volta a ficar oculta ao fechar o modal, limpar o formulário ou ocultar a aba.
- O componente altera somente o tipo do campo. Não grava valores em armazenamento local nem em logs. O servidor continua usando scrypt com salt individual; APIs de sessão e usuários não retornam senha ou hash. Editar usuário permite somente informar uma nova senha.
- Link “Ir para o conteúdo principal”, foco visível, nomes de ações nas tabelas, campos obrigatórios identificados e erros associados aos campos.
- Modais nativos com título acessível, foco inicial no título, ciclo de Tab/Shift+Tab, Esc e retorno ao acionador. Durante uma gravação, o fechamento aguarda a conclusão para evitar envios ambíguos.
- Tipografia de tela ampliada, contraste revisto, controles com pelo menos 44 px, menu que se reorganiza em telas estreitas e respeito a movimento reduzido. Tabelas mantêm sua semântica e rolagem local por teclado; a página não exige rolagem horizontal. As dimensões físicas das etiquetas impressas não mudam.
- Leitura de QR com alternativa manual, identificação por texto, orientações para câmera e aviso quando a leitura não é obtida. Mensagens de sucesso permanecem disponíveis até a próxima operação.
- Aviso cinco minutos antes do vencimento da sessão e botão “Continuar conectado”, inclusive no modal aberto. A renovação exige sessão válida e CSRF; não recupera sessões já expiradas ou revogadas.

## Testes reproduzíveis

Na pasta acima de `Pharmacia`:

```powershell
python -m unittest discover -s Pharmacia -p "test_*.py"
python Pharmacia/browser_lot_check.py
python Pharmacia/browser_accessibility_check.py
```

O último teste usa Edge instalado, Playwright 1.58.0 em `Pharmacia/test_tools` e axe-core 4.10.3 em `Pharmacia/test_tools/axe.min.js`. Essas dependências são somente de teste, não são servidas pelo sistema e não precisam ser publicadas. Todos os testes usam banco temporário e dados fictícios.

A auditoria axe cobre login, redefinição, criação de usuário, alteração de senha, dashboard, medicamentos, lotes, movimentações, transferências, inventário, alertas, relatórios, requisições, perdas, reposição, histórico, prévia de etiquetas e saída rápida com erro. O resultado fica em `accessibility-test-results.json`.

O teste de navegador aciona Enter/Espaço, percorre Tab/Shift+Tab em modais, fecha com Esc, verifica retorno do foco, realiza saída, envio/recebimento de transferência, contagem e download de relatório. Também verifica campos obrigatórios, quantidade inválida, renovação de sessão sem perder preenchimento e nomes/papéis na árvore de acessibilidade do Edge.

São testadas larguras de 320 a 1440 px, com retrato e paisagem. As larguras de 1280, 853, 640 e 320 px exercitam o rearranjo equivalente a 100%, 150%, 200% e 400% em uma janela de 1280 px. Isso não substitui teste manual do zoom do navegador.

## Validação assistiva e em equipamentos reais ainda necessária

A árvore de acessibilidade e o axe são apoio. Não comprovam a experiência real com leitor de tela, impressão, câmera ou toque. Antes de declarar conformidade AA e liberar para uso, executar e registrar:

1. NVDA com Edge/Firefox no Windows e VoiceOver com Safari no celular: ouvir rótulos, obrigatoriedade, erros, estado do olho, título do modal, cabeçalhos das tabelas, leitura do QR e confirmação das operações. Confirmar que a senha oculta não é anunciada como texto comum.
2. Navegar sem mouse do login até saída, transferência, inventário e relatório; usar também o link de salto, seleção de unidade, filtros e tabelas roláveis. Confirmar foco sempre visível e retorno ao local correto.
3. Usar zoom nativo a 100%, 150%, 200% e 400%, inclusive com mensagens longas e nomes extensos, verificando sobreposição, corte e acesso aos botões.
4. Em celular e tablet reais, nas duas orientações, testar toque no olho, teclado virtual, câmera permitida/negada, QR legível/ilegível, alternativa manual e feedback textual.
5. Conferir contraste e compreensão dos estados com usuários com baixa visão/daltonismo; testar modo de alto contraste do sistema operacional.

Não registrar senhas, tokens, informações de pacientes ou dados reais nos resultados de teste.
