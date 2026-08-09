# WG Kill Switch

Menu-bar kill-switch для официального приложения **WireGuard** на macOS.

Когда включён — весь исходящий трафик, который идёт мимо активного туннеля WireGuard, блокируется (PF). Если WireGuard отвалился, интернет остаётся закрыт, а вы получаете уведомление. Профиль WireGuard определять вручную не нужно: демон сам смотрит, что сейчас подключено.

## Как это работает

```
┌─────────────────┐     desired.json      ┌──────────────────┐
│  Иконка в меню  │ ───────────────────► │  wgksd (root)    │
│  WGKillSwitch   │ ◄─────────────────── │  LaunchDaemon    │
└─────────────────┘     status.json       └────────┬─────────┘
                                                   │
                     scutil (профили WG)           │
                     ifconfig / route              │
                                                   ▼
                                            PF anchor
                                         wgkillswitch
```

1. **Menu bar app** — kill-switch, отдельный переключатель Tailscale, статус, уведомления.
2. **Демон `wgksd`** (root) — раз в ~1 с читает WireGuard/`scutil`, детектит Tailscale, обновляет PF.
3. **Любой активный профиль WG** — endpoint’ы всех профилей для handshake, трафик через текущий WG `utun`.
4. **Tailscale (опционально)** — отдельный флаг «Работать с Tailscale». Демон смотрит, **установлен ли клиент** (`Tailscale.app` / `scutil`), а не список пиров в сети.
5. **Выключение** — PF возвращается к системному `/etc/pf.conf`.

### Что разрешено при включённом kill-switch

- loopback
- DHCP (чтобы Wi‑Fi мог подняться)
- UDP handshake до endpoint’ов WireGuard (все известные профили)
- весь трафик через активный интерфейс туннеля WG
- если включено **Работать с Tailscale** — overlay Tailscale (`utun` с `100.64/10`, MagicDNS)

Clearnet мимо WireGuard — drop.

## Требования

- macOS 13+
- установленный [WireGuard для macOS](https://apps.apple.com/app/wireguard/id1451685025)
- Xcode Command Line Tools (`xcode-select --install`) — нужны для сборки Swift-приложения
- права администратора на установку (LaunchDaemon + `/Applications`)

## Установка

```bash
git clone https://github.com/httpapassha/WGKillSwitch.git
cd WGKillSwitch
./install.sh
```

Скрипт:

1. Соберёт `WGKillSwitch.app`
2. Запросит пароль администратора (диалог macOS)
3. Поставит демон, CLI и приложение
4. Включит автозапуск (демон при загрузке, иконка при логине)

После установки в меню-баре появится щит.

## Использование

| Действие | Как |
|----------|-----|
| Включить / выключить KS | Меню-бар → «Включить/Выключить Kill Switch» |
| Tailscale при KS | Меню-бар → галочка **«Работать с Tailscale»** (по умолчанию вкл.) |
| Статус | Пункты в том же меню (установлен ли TS, интерфейс) |
| Выйти из приложения | Quit (демон kill-switch продолжает работать) |
| CLI | см. ниже |

```bash
wgksctl enable
wgksctl disable
wgksctl toggle
wgksctl tailscale-on
wgksctl tailscale-off
wgksctl tailscale-toggle
wgksctl status
```

### Типичный сценарий

1. Подключите любой профиль в приложении WireGuard.
2. Включите Kill Switch из меню.
3. Если WG отключить — трафик блокируется, приходит уведомление.
4. Когда WG снова подключится — интернет возвращается сам (через туннель).
5. Kill Switch можно выключить в любой момент из меню или `wgksctl disable`.

## Автозапуск

| Компонент | Механизм | Когда |
|-----------|----------|--------|
| `wgksd` | `/Library/LaunchDaemons/com.local.wgkillswitch.daemon.plist` | загрузка системы |
| Menu bar | `~/Library/LaunchAgents/com.local.wgkillswitch.agent.plist` | логин пользователя |

Если закрыли иконку через Quit и хотите вернуть до перезагрузки:

```bash
open -a WGKillSwitch
```

## Удаление

```bash
./uninstall.sh
```

Снимает приложение, демон, LaunchAgent/Daemon, CLI и восстанавливает системный PF.

## Структура репозитория

```
WGKillSwitch/
├── app/                 # menu bar (Swift / AppKit)
├── daemon/
│   ├── wgksd.py         # root-демон (PF + мониторинг WG)
│   ├── wgksctl.py       # CLI enable/disable/status
│   └── *.plist
├── build.sh             # сборка .app
├── install.sh           # установка «из коробки»
├── install-root.sh      # привилегированная часть установки
└── uninstall.sh
```

## Безопасность и ограничения

- Kill-switch реализован через системный **PF**, не через скрытую опцию WireGuard.app (в официальном клиенте на macOS её нет).
- Демон работает от root — это нужно для `pfctl`.
- `desired.json` доступен на запись пользователю, чтобы UI мог переключать режим без пароля каждый раз.
- Clearnet (TCP/UDP мимо WG) режется на физических интерфейсах при KS ON.
- При **Работать с Tailscale** дополнительно разрешены: Tailscale `utun`, UDP/41641 и DNS/53 на физике (нужно для MagicDNS и админ-консоли в браузере). Без DNS/53 сайт `console.tailscale.com` зависает, хотя `dig @1.1.1.1` работает.
- Это не App Store-сборка и не notarized binary: Gatekeeper может спросить подтверждение при первом запуске на другом Mac.

## Лицензия

MIT
