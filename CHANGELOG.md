# Changelog

What each release brings. Newest first. · Что приносит каждый выпуск, новые сверху.

## Unreleased · Готовится

## 0.1.24

- An agent can be marked free on purpose: the lead with `collab rest <id> [--until HH:MM] [--why …]`, you in your chat with `/rest`; the lead's round leaves it be until the mark ends, and `/team` shows it. · Агента можно отметить свободным намеренно: ведущий — `collab rest <id> [--until ЧЧ:ММ] [--why …]`, вы — в своём чате `/rest`; обход ведущего его не трогает, пока метка не кончится, `/team` её показывает.
- With no goal set, the lead's round tells you once that it cannot see free agents; an agent free longer than two rounds while the goal is open is told to the lead once more. · Если цель не задана, обход один раз говорит вам, что не видит свободных агентов; об агенте, свободном дольше двух обходов при открытой цели, ведущему говорится ещё раз.
- /clear after a landing is given only to a Claude whose memory is «clear after handing in», and not while it still holds a task. · /clear после влития получает только Claude с памятью «очищать после сдачи», и не пока у него взятая задача.
- A Codex turn with a background terminal («… esc to interrupt) · 1 background terminal running») is read as busy: nothing is typed into it. · Ход Codex с фоновым терминалом («… esc to interrupt) · 1 background terminal running») читается как занятый: в него ничего не печатается.

## 0.1.23

- collab starts about twice as fast when run as a command (and so do the Claude hooks): the compiled program is kept in `~/.collab-room/code`, checked by the hash of its code and by its owner and rights; anything else is compiled as before. · collab запускается примерно вдвое быстрее командой (и хуки Claude тоже): скомпилированная программа хранится в `~/.collab-room/code`, сверяется по хешу кода, владельцу и правам; иначе компилируется как раньше.
- «Clear after handing in» waits while the agent still holds a task; after any /clear or /compact the agent is told where it was — its task, its last message, its plan file. · «Очищать после сдачи» ждёт, пока у агента взятая задача; после любого /clear или /compact агенту напоминается, где он был — задача, его последнее сообщение, файл плана.
- The lead's round tells of an agent whose screen has not changed for 20 minutes while it works on a task, with its last lines; a busy agent (working, in background, landing) is called neither silent nor free. · Обход ведущего сообщает об агенте, чей экран не менялся 20 минут при взятой задаче, с последними строками; занятого агента (работает, фон, влитие) обход не называет ни молчащим, ни свободным.
- The full test suite runs on four processes when the machine is free; a test run gives a short summary, its output stays in a file. · Полный набор тестов идёт в четыре процесса, когда машина свободна; итог прогона — коротко, вывод остаётся в файле.

## 0.1.22

- Telegram `/control`: an action that failed and changed nothing is told apart from one whose end is unknown («could not confirm — do not repeat»); the final card names who pressed. · Telegram `/control`: действие, которое не удалось и ничего не изменило, отличается от действия с неизвестным исходом («не удалось подтвердить — не повторяйте»); на итоговой карточке — кто нажал.
- The room's log line for an agent restarted from Telegram says who and that the saved conversation went on. · Строка журнала о перезапуске агента из Telegram говорит, кто перезапустил и что сохранённый разговор продолжен.

## 0.1.21

- A Codex agent can send you files with `collab say --file`, under the same checks as everyone else. · Агент Codex может присылать вам файлы через `collab say --file` — с теми же проверками, что у всех.
- A file from Telegram is saved under a name the room makes; its own name is only shown, never given as a path. · Файл из Telegram сохраняется под именем, которое даёт комната; его собственное имя только показывается и никогда не идёт путём.
- A permission question is read more strictly: a long line the terminal wrapped can no longer hide part of the command on its card. · Вопрос о разрешении читается строже: длинная строка, перенесённая терминалом, больше не может спрятать часть команды на карточке.

## 0.1.20

- Telegram: an album comes to the agent as one message, files in order, captions joined. · Telegram: альбом приходит агенту одним сообщением — файлы по порядку, подписи вместе.
- Telegram: after a video, «Prepare for analysis» asks the agent it went to for up to 20 frames with timecodes and a manifest of what is skipped, no sound; the room itself runs nothing on the video. · Telegram: после видео кнопка «Подготовить для анализа» просит агента-адресата сделать до 20 кадров с таймкодами и список пропусков, без звука; сама комната на видео ничего не запускает.

## 0.1.19

- Telegram: «Agent's screen» in `/control` — the text of the chosen agent's window, without colours, comes to the topic as a .txt file. Only the agent's own window, never your chat. · Telegram: «Снимок экрана агента» в `/control` — текст окна выбранного агента, без цветов, приходит в тему файлом .txt. Только окно самого агента, никогда ваш чат.
- `collab say --file` checks the file again once it is open, so a folder swapped for a link in between does not pass. · `collab say --file` проверяет файл ещё раз уже открытым — папка, подменённая ссылкой в промежутке, не проходит.

## 0.1.18

- `collab say --file`: an agent sends you a file of its project (up to 50 MB, up to ten at once) — in the room it is shown as «📎 name · size», in Telegram a picture comes as a photo, an MP4 as a video, anything else as a document. Only ordinary files inside the project: no links out, no hidden folders, nothing named like a secret. For now Claude agents and you can send files; a Codex agent cannot. · `collab say --file`: агент присылает вам файл своего проекта (до 50 МБ, до десяти за раз) — в комнате он виден как «📎 имя · размер», в Telegram картинка приходит фото, MP4 — видео, остальное — документом. Только обычные файлы внутри проекта: без ссылок наружу, скрытых папок и имён, похожих на секреты. Пока файлы могут слать агенты Claude и вы; агент Codex — нет.

## 0.1.17

- Telegram: send a photo, a screenshot, a document, a video or audio (up to 20 MB) to the room's topic; it goes to the agent the words would go to, saved in the project under `collab-inbox/`, and the agent opens it with its own tools. A file is data, never instructions. · Telegram: пришлите в тему комнаты фото, скриншот, документ, видео или аудио (до 20 МБ); файл уходит тому агенту, кому ушли бы слова, сохраняется в проекте в `collab-inbox/`, и агент открывает его своими инструментами. Файл — данные, не указания.

## 0.1.16

- Telegram: when an agent waits in its window for a permission, its topic gets the question and every option word for word, a button for each. A press is checked against the window again — the same agent, the same question — and sends one key; otherwise nothing is pressed. Only for program versions the room knows; any other question stays «needs you at the computer». · Telegram: когда агент ждёт в окне разрешения, в его тему приходит вопрос и все варианты дословно, по кнопке на каждый. Нажатие сверяется с окном заново — тот же агент, тот же вопрос — и посылает одну клавишу; иначе ничего не нажимается. Только для известных комнате версий программ; любой другой вопрос — по-прежнему «нужно действие на компьютере».

## 0.1.15

- Telegram: `/control` in a room's topic — pause or resume delivery in the room (with how many messages wait), restart an agent so its saved conversation goes on. Each action asks for a confirmation and runs once; a card older than ten minutes does nothing. · Telegram: `/control` в теме комнаты — приостановить или возобновить доставку (видно, сколько сообщений ждут), перезапустить агента — его сохранённый разговор продолжится. Каждое действие просит подтверждения и выполняется один раз; карточка старше десяти минут ничего не делает.

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
