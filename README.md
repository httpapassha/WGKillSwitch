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

1. **Menu bar app** — Kill Switch + галочка **Работать с Tailscale**, статус, уведомления.
2. **Демон `wgksd`** — WireGuard через `scutil`, детект установленного Tailscale (не список пиров), PF.
3. **Профили WG** — handshake allow-list по всем endpoint’ам, трафик через активный WG `utun`.
4. **Tailscale (опционально)** — control plane (`192.200.0.0/24` + нужные хосты), MagicDNS upstream, UDP/41641; clearnet всё равно только через WG.
5. **Выключение** — PF → `/etc/pf.conf`.

### Что разрешено при включённом kill-switch

- loopback, DHCP
- UDP handshake до WG endpoint’ов
- весь трафик через WG `utun`
- если **Работать с Tailscale**: TS `utun`, UDP/41641, TCP/443 к control/admin Tailscale, DNS к 1.1.1.1/8.8.8.8/… для MagicDNS

Остальной clearnet с Wi‑Fi/Ethernet — drop.

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
| Kill Switch | Меню → Включить / Выключить |
| Tailscale | Меню → галочка **Работать с Tailscale** (по умолчанию вкл.) |
| Quit | только UI; демон KS продолжает работать |

```bash
wgksctl enable|disable|toggle
wgksctl tailscale-on|tailscale-off|tailscale-toggle
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
- При выключенной галочке Tailscale — строгий режим (только WG). При включённой — узкие исключения под control plane/MagicDNS/peers, не весь интернет.
- Это не App Store-сборка и не notarized binary: Gatekeeper может спросить подтверждение при первом запуске на другом Mac.

## Лицензия

MIT
