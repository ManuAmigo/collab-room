# Changelog

What each release brings. Newest first. · Что приносит каждый выпуск, новые сверху.

## Unreleased · Готовится

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
