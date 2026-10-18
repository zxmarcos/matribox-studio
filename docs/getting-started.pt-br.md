# Primeiros passos: seu primeiro firmware customizado

*[English version](getting-started.md)*

Este guia leva você de uma Matribox II Pro de fábrica a um firmware customizado com:

- **cadeia dupla**: dois caminhos de efeitos em paralelo no mesmo preset, desenhados na tela do pedal em duas
  fileiras;
- **listas de texto**: knobs que mostram nomes ("Tremolo", "Vibrato"…) em vez de números;
- **capturas NAM**: capturas reais do [Neural Amp Modeler](https://www.tone3000.com) tocando no menu Clone;
- **efeitos novos**: os efeitos de exemplo deste projeto e, se quiser, efeitos portados do Valeton GP-200 e da
  Matribox II (QME-100).

Não é preciso programar nem instalar nada além do Studio.

> **Antes de começar.** Gravar um firmware customizado é por sua conta e risco, mas dá para voltar atrás: o
> firmware oficial sempre pode ser regravado pelos mesmos passos (veja
> [Voltar ao firmware oficial](#voltar-ao-firmware-oficial)). **Toda atualização de firmware substitui os
> presets do pedal** pelos que estão no arquivo; se você tem presets que não quer perder, guarde-os antes (pelo
> editor oficial, ou anotando os ajustes). As capturas NAM e os User IRs são mantidos.

## O que você precisa

- Uma Matribox II Pro e o cabo USB dela.
- Um PC com Windows.
- O firmware oficial **`QME-200_FW_USB_V1.2.2.bin`**, do site da Sonicake (página de downloads da Matribox II
  Pro). A versão V1.2.2 importa: as ferramentas são feitas para ela.
- Para o NAM: uma ou mais capturas **A2** (arquivos `.nam`). Veja o [passo 6](#passo-6-gravar-capturas-nam).

## Passo 1: instalar o Studio

1. Baixe o `MatriboxStudio-windows.zip` na
   [página de releases](https://github.com/zxmarcos/matribox-studio/releases).
2. Descompacte numa pasta à sua escolha (por exemplo `Documentos\MatriboxStudio`). Não rode de dentro do zip.
3. Copie o `QME-200_FW_USB_V1.2.2.bin` para a pasta **`firmware`**, que fica ao lado do `MatriboxStudio.exe`.
4. Abra o **`MatriboxStudio.exe`**. O Windows pode avisar que o aplicativo é de um editor desconhecido: clique em
   "Mais informações" e "Executar assim mesmo".

O Studio abre na aba **Firmware**, com o firmware oficial como ponto de partida ("Base", na barra de baixo).

## Passo 2: gerar uma base com os recursos novos

Os recursos que mudam o funcionamento do pedal (cadeia dupla, listas de texto, NAM no menu Clone) são patches no
programa do pedal. O Studio os aplica ao firmware oficial e salva o resultado como uma nova **base**.

1. Na aba Firmware, clique no botão **⋯** no canto superior direito e escolha **"Gerar base do original…"**.
2. "Firmware original" já aponta para o seu `.bin`. Em **"PATCHES DO APP"**, ligue:
   - **"GUI paralela"**: a cadeia dupla desenhada em duas fileiras;
   - **"Listas de texto"**: knobs com opções nomeadas;
   - **"Clone com dois motores"**: cada slot do Clone toca uma captura de fábrica ou uma captura NAM;
   - **"Abas do Clone"**: o menu Clone ganha as abas "Native" e "NAM A2", para achar cada tipo mais rápido.
3. Deixe **"Adicionar Split / Path B / Merge"** ligado (a cadeia dupla precisa desses três blocos; o Studio liga
   sozinho).
4. Clique em **"Gerar base"**. Em poucos segundos aparece "Base gerada". Clique em **"Usar como base"**.

A barra de baixo passa a mostrar a nova base, por exemplo
`QME-200_FW_USB_V1.2.2_parallel_textlist_clonedual_clonetabs.bin`. Isso só precisa ser feito uma vez.

## Passo 3 (opcional): extrair efeitos de outros pedais

O Valeton GP-200 e a Matribox II (QME-100) usam a mesma plataforma e têm efeitos que a Pro não tem (drives, amps,
delays e reverbs como Bad-KT, Silver Twin, Ice Delay e Tube Spring). Dá para trazê-los a partir do firmware
oficial de cada um.

1. Baixe o firmware oficial do outro pedal no site do fabricante: `GP-200_USB_Firmware_V1.8.2.bin` e/ou
   `QME-100_FW_USB_V1.3.0.bin`.
2. Abra a aba **Ferramentas** e escolha o pedal à esquerda. Se for fazer os dois, faça o GP-200 primeiro.
3. Escolha o arquivo em **"FIRMWARE ORIGINAL"**. Uma linha verde quer dizer que é exatamente o arquivo para o
   qual a receita foi feita.
4. Clique em **"Extrair efeitos"** e espere cerca de um minuto e meio. A tabela vai sendo preenchida conforme cada
   efeito é convertido.

Os efeitos extraídos entram nas bibliotecas do Studio e aparecem na aba Firmware, com a origem "Valeton GP-200"
ou "Matribox II (QME-100)".

## Passo 4: escolher os efeitos

De volta à aba **Firmware**, a lista mostra todos os efeitos: os que já estão na base (origem "Fábrica") e os das
bibliotecas (Tutorial, Próprios, Roteamento e os pedais portados).

- **Marque** um efeito para colocá-lo no firmware; **desmarque** para deixá-lo de fora. Os efeitos das bibliotecas
  começam desmarcados.
- Marque pelo menos o **Multi Mode** (origem "Próprios"): é o efeito que mostra as listas de texto funcionando.
  Mod Echo e Gain Boost são outros exemplos.
- Use os **menus à esquerda** e a **busca** para achar efeitos; "Alterações" mostra só o que você mudou.

A barra de baixo ("BIBLIOTECA DSP") mostra quanto da memória de efeitos do pedal está em uso: 2,5 MB no total,
a maior parte ocupada pelos efeitos de fábrica. Se uma parte dela ficar vermelha, a seleção não cabe: desmarque
alguns efeitos, por exemplo efeitos de fábrica que você nunca usa (o Studio avisa se algum preset de fábrica usa
o efeito). O indicador à direita mostra **"Tudo certo"** quando a seleção pode ser gerada.

**"Salvar perfil"** guarda a seleção num arquivo, para reabrir depois com "Abrir perfil…" no menu ⋯. O Studio
também lembra dela quando você fecha o programa.

## Passo 5: gerar e enviar o firmware

1. Clique em **"Gerar firmware"** (ou Ctrl+G).
2. Em **"MOTOR DO CLONE"**, escolha **"Fábrica + NAM A2 (automático por slot)"**: assim cada slot do Clone toca a
   captura que tiver, de fábrica ou NAM.
3. Se você usa o editor oficial de PC, ligue **"Gerar também a lista do editor de PC"**: ele grava um
   `alg_data.json` ao lado do `.bin`, com a nova lista de efeitos para o editor (copie-o para
   `data\flutter_assets\assets\alg\` de uma cópia do editor). Se você só usa o Studio e o pedal, pode pular.
4. Clique em **"Gerar firmware"**. Quando aparecer "Firmware gerado", clique em **"Enviar ao pedal"**.
5. **Feche o editor oficial de PC** se estiver aberto (só um programa conversa com o pedal por vez), conecte o
   pedal pela USB e ligue-o.
6. Na janela "Atualizar o pedal", clique em **"Atualizar pedal"** e confirme. O pedal reinicia no modo de
   atualização e o firmware é gravado. **Não desconecte nem desligue o pedal** até aparecer "Firmware gravado".
   Depois ele reinicia com o firmware novo.

Se algo falhar, o pedal continua no modo de atualização e é só clicar em "Tentar de novo".

## Passo 6: gravar capturas NAM

O pedal toca capturas NAM da arquitetura **A2** (NAM 0.7 em diante; no [TONE3000](https://www.tone3000.com)
procure capturas A2). As capturas "standard", mais antigas, não rodam no pedal.

1. Abra a aba **Capturas NAM** e clique em **"Conectar"** no canto superior direito, se o pedal ainda não estiver
   conectado.
2. Clique em **"Pasta"** e escolha a pasta com seus arquivos `.nam`. A lista diz quais capturas rodam no pedal.
3. À esquerda, escolha um **slot do Clone** (1 a 10). Na lista, escolha uma captura.
4. À direita, dê um nome e clique em **"Gravar no Clone N"**. O Studio avisa antes de substituir um slot.

No pedal, adicione um bloco **Clone** (menu CLN) a um preset e escolha o slot: a aba **"NAM A2"** lista os slots
com capturas NAM, e a **"Native"**, os de fábrica. Uma captura de 48 kHz é reamostrada em tempo real, então soa
como deve.

## Passo 7: usar os recursos novos

### Cadeia dupla

![Uma cadeia dupla no Studio](images/studio.png)

A cadeia dupla é montada com três blocos do menu **EQ**, aba "Routing":

```
IN -> [Split] -> efeitos do caminho A -> [Path B] -> efeitos do caminho B -> [Merge] -> OUT
```

1. Abra a aba **Pedal e presets**, clique em **"Conectar"** e escolha um preset à esquerda. As edições tocam no
   pedal na hora.
2. Em **"EFEITOS"**, clique em **EQ** e adicione o **Split**, depois os efeitos do caminho A, depois o **Path B**,
   depois os efeitos do caminho B e, por fim, o **Merge**. Clique num efeito para adicioná-lo ao fim da cadeia, ou
   arraste-o para o ponto da cadeia onde ele deve entrar. Arraste os blocos para reordenar; Delete remove o bloco
   selecionado.
3. A cadeia aparece em duas fileiras, no Studio e na tela do pedal.
4. Clique no **Merge** para ajustar a mistura: Level A / Level B, Pan A / Pan B e Phase B. O Mode do Split escolhe
   o que vai para os caminhos (Mono L, Mono R ou Stereo).
5. Salve o preset pelo botão **SAVE** do próprio pedal. "Descartar", no Studio, desfaz as edições.

Usos típicos: dois amps ao mesmo tempo, um caminho limpo ao lado de outro cheio de efeitos, compressão paralela.

Uma seção dupla por preset. Os três blocos de roteamento ocupam 3 dos 12 slots de um preset.

### Listas de texto

Adicione o **Multi Mode** (menu MOD, aba "Special") a um preset. O knob Mode mostra **Tremolo, Vibrato, Ring Mod,
Bitcrush, Octave Fuzz** no pedal e no Studio, em vez de 0 a 4. Qualquer efeito cujo manifesto tenha opções
nomeadas as mostra do mesmo jeito.

## Voltar ao firmware oficial

Clique em **"Atualizar pedal"** no topo da aba Firmware, escolha o `QME-200_FW_USB_V1.2.2.bin` oficial e clique em
"Atualizar pedal". Tudo volta ao de fábrica (inclusive os presets); as capturas NAM e os User IRs continuam nos
slots.

Se um dia o pedal não iniciar, ele ainda pode ser atualizado: segure, ao ligá-lo, a combinação de footswitches
que as instruções da Sonicake indicam para o modo de atualização, e grave o arquivo oficial do mesmo jeito. As
atualizações normais nunca mexem na parte do pedal que faz isso, então ela está sempre disponível.

## Próximos passos

Estas páginas estão em inglês:

- [Escrever seu próprio efeito em C](effects/tutorial.md).
- [Como os patches funcionam](app-patches.md), [NAM no pedal](nam.md), [os ports](ports.md).
- [O Studio e as ferramentas de linha de comando](tools.md).
