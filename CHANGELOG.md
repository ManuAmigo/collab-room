# Changelog

What each release brings. Newest first. · Что приносит каждый выпуск, новые сверху.

## Unreleased · Готовится

## 0.1.14

- Telegram: write to any agent of the room, not only the lead — `/dm` lists them; Reply to an agent's answer goes to that agent. Plain text still goes to the lead. Every agent's words to you come to the room's topic, signed; what you write from the computer is shown there too. · Telegram: пишите любому агенту комнаты, не только ведущему — `/dm` показывает список; Reply на ответ агента уходит этому агенту. Обычный текст — по-прежнему ведущему. Всё, что агенты пишут вам, приходит в тему комнаты с подписью; написанное вами с компьютера тоже видно там.
- `collab doctor`: what runs where — the command, the chats and windows of each room, their versions — as text to paste; `--fix` repairs what it can, asking yes for each. An open chat older than the installed collab is named, with a restart offered. · `collab doctor`: что где запущено — команда, чаты и окна каждой комнаты, их версии — текстом, который можно вставить; `--fix` чинит, что может, спрашивая «да» на каждое. Открытый чат старее установленного collab назван, предложен перезапуск.
- «Lead» in an agent's card makes it the room's lead. · «Ведущий» в карточке агента делает его ведущим комнаты.
- `/language` applies at once; «and for new rooms» is a separate choice. In a Russian room the labels around delivered messages are Russian. · `/язык` применяется сразу; «и для новых комнат» — отдельный пункт. В русской комнате подписи вокруг доставленных сообщений — на русском.
- A question's hint is shown above the menu it opens, never hidden under it. · Подсказка вопроса — над меню, которое он открывает, а не под ним.

## 0.1.13

- Telegram: `/telegram` in the room's chat links your account to the room's bot — a link that opens it with the code, a QR code to scan, or the code itself; each room gets a topic of its own. · Telegram: `/телеграм` в чате комнаты привязывает ваш аккаунт к боту — ссылкой, которая открывает его с кодом, QR-кодом или самим кодом; у каждой комнаты своя тема.
- Write to the lead from its topic, Reply to answer a question; the lead's answers, questions, proposals and the goal reached come back there. A message from Telegram is never taken as the go-ahead for a change of the team. · Пишите ведущему в теме комнаты, Reply — ответ на вопрос; ответы, вопросы и предложения ведущего и «цель достигнута» приходят туда же. Сообщение из Telegram никогда не считается разрешением менять команду.
- «Needs you»: an agent waiting for you in its window is named in the room's topic; the room's pause is said there too. · «Нужен ты»: агент, который ждёт вас в своём окне, назван в теме комнаты; там же — о паузе комнаты.
- `/status` in a room's topic and `/rooms` outside them show where each room is and what waits for you. · `/status` в теме комнаты и `/rooms` вне тем показывают, где каждая комната и что ждёт вас.
- `collab telegram limit N`: past N tokens in a day, one line a day with the spend by agent. · `collab telegram limit N`: выше N токенов за день — одна строка в день с расходом по агентам.
- When all is well the bot says nothing: a 👀 on your message once the room has it; only failures are said in words. · Когда всё в порядке, бот молчит: 👀 на вашем сообщении, когда комната его взяла; словами — только сбои.

## 0.1.12

- A Codex agent can lead a room: it proposes team changes and declares the goal reached through its narrow rule; every change it proposes waits for your Enter. · Агент Codex может вести комнату: предлагает изменения команды и объявляет цель достигнутой через своё узкое правило; каждое его предложение ждёт вашего Enter.

## 0.1.11

- A Codex agent's usage is counted again after it is restarted. · Расход агента Codex снова учитывается после его перезапуска.
- The lead's proposal card opens only after the messages that came before it are on screen. · Карточка предложения ведущего открывается только после того, как на экране всё, что пришло раньше.
- A change of a ready team's topic is shown in the room's language. · Смена темы из готового сценария показывается на языке комнаты.
- «Clear after handing in» works for an agent that reports to you directly, with no lead. · «Очищать после сдачи» работает и у агента, который отвечает вам напрямую, без ведущего.

## 0.1.10

- A ready team's topics are shown in the room's language; topics you typed stay as written. · Темы готовых сценариев — на языке комнаты; темы, введённые вами, — как написаны.

## 0.1.9

- Narrow terminals: words are no longer broken mid-word, long rows end with «…». · Узкое окно: слова не рвутся посередине, длинные строки заканчиваются «…».
- The screens before a room opens (first menu, folder choice, yes/no) follow the chosen language. · Экраны до входа в комнату (первое меню, выбор папки, да/нет) — на выбранном языке.
- `/help` in a Russian room no longer fails. · `/помощь` в русской комнате больше не падает.

## 0.1.8

- `collab update` and the update at `collab start` also update the Claude Code plugin, and say Claude agents need `/restart` for its new hooks. · Обновление collab обновляет и плагин Claude Code и говорит, что агентам Claude нужен `/restart`.

## 0.1.7

- Your own scenario: save a running team from `/team` and pick it when creating a new room. · Свой сценарий: сохранить работающую команду из `/команда` и выбрать её при создании комнаты.
- The lead's round: the room wakes the lead only when there is a reason (a silent worker, an idle agent, unanswered work), once per round, until the goal is reached. · Обход ведущего: комната будит ведущего только при поводе (молчит исполнитель, кто-то без задачи, работа без ответа), раз за обход, пока цель не достигнута.

## 0.1.6

- Russian everywhere: the chat's steps, feed, panels, the command line, the installer and errors. · Русский везде: шаги чата, лента, панели, командная строка, установка и ошибки.
- A ready team can be edited before launch: change, add or remove agents. · Готовый состав можно изменить до запуска: поменять, добавить или убрать агентов.
- «Clear» memory clears an agent's context after it hands in its work. · Память «Очищать» очищает контекст агента после сдачи работы.

## 0.1.5

- English or Russian: chosen at the first start, `/language` (`/язык`) in a room; Russian names for all chat commands. · Английский или русский: выбор при первом запуске, `/язык` в комнате; русские имена всех команд чата.

## 0.1.4

- Six ready teams — Development, Research, Text, Decision, Long work, One assistant — or no template at all. · Шесть готовых сценариев — Разработка, Исследование, Текст, Решение, Долгая работа, Один помощник — или без шаблона.
- `/team`: see and change a running team; before → after, when it takes effect, a history with undo. · `/команда`: посмотреть и изменить работающую команду; «было → станет», когда подействует, история с отменой.
- The lead proposes changes, you apply them with one Enter. · Ведущий предлагает изменения, вы применяете их одним Enter.
- A long message is no longer left unsent as a pasted draft in Claude Code. · Длинное сообщение больше не застревает черновиком-вставкой в Claude Code.

## 0.1.3

- Five settings per agent: role, topic, model, memory, whom it reports to. · Пять настроек агента: роль, тема, модель, память, кому отвечает.
- Each agent gets its assignment on joining, after `/restart` and after its context is compacted or cleared. · Агент получает назначение при входе, после `/restart` и после сжатия или очистки контекста.
- Optional context trim: past a threshold an idle agent is given `/compact` or `/clear`. · По желанию: после порога свободному агенту даётся `/compact` или `/clear`.

## 0.1.2

- `collab add` starts an agent in a running room without the chat. · `collab add` запускает агента в работающей комнате без чата.
- Agents' messages to an idle agent are gathered and delivered in one wake. · Сообщения агентов свободному агенту собираются и доставляются за одно пробуждение.
- A scrolled agent window says «Scrolled — q to exit». · Прокрученное окно агента показывает «Просмотр истории — q для выхода».

## 0.1.1

- collab updates itself at `collab start` when a newer release is out. · collab обновляется сам при `collab start`, если вышла новая версия.

## 0.1.0

- First public release. · Первый публичный выпуск.
