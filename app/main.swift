import AppKit
import Foundation
import UserNotifications

struct ProfileInfo: Decodable {
    let uuid: String
    let name: String
    let connected: Bool
    let endpoint: String?
}

struct Status: Decodable {
    let enabled: Bool
    let allowTailscale: Bool?
    let tailscaleInstalled: Bool?
    let tailscaleConnected: Bool?
    let tailscaleActive: Bool?
    let tailscaleInterfaces: [String]?
    let pfOk: Bool
    let pfMessage: String?
    let wgConnected: Bool
    let tunnelReady: Bool?
    let activeProfile: String?
    let interface: String?
    let interfaces: [String]?
    let endpoints: [String]
    let profiles: [ProfileInfo]
    let blocking: Bool
    let unhealthy: Bool
    let icon: String
    let updatedAt: String?
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private var refreshTimer: Timer?
    private var lastStatus: Status?
    private var lastMenuFingerprint = ""
    private var lastNotifiedUnhealthy = false
    private var menuIsOpen = false

    private let statusURL = URL(fileURLWithPath: "/Library/Application Support/WGKillSwitch/status.json")
    private let ctlPath = "/usr/local/bin/wgksctl"

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "shield", accessibilityDescription: "WG Kill Switch")
            button.image?.isTemplate = true
            button.toolTip = "WG Kill Switch"
        }

        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        rebuildMenu(status: nil, menu: menu)
        refresh()

        refreshTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        if let refreshTimer {
            RunLoop.main.add(refreshTimer, forMode: .common)
        }
    }

    private func readStatus() -> Status? {
        guard let data = try? Data(contentsOf: statusURL) else { return nil }
        return try? JSONDecoder().decode(Status.self, from: data)
    }

    @discardableResult
    private func runCtl(_ args: String...) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ctlPath)
        proc.arguments = Array(args)
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus == 0
        } catch {
            let proc2 = Process()
            proc2.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            proc2.arguments = ["/usr/local/libexec/wgkillswitch/wgksctl.py"] + Array(args)
            proc2.standardOutput = Pipe()
            proc2.standardError = Pipe()
            do {
                try proc2.run()
                proc2.waitUntilExit()
                return proc2.terminationStatus == 0
            } catch {
                return false
            }
        }
    }

    private func refresh() {
        let status = readStatus()
        lastStatus = status
        updateIcon(status)
        if !menuIsOpen {
            let fp = menuFingerprint(status)
            if fp != lastMenuFingerprint, let menu = statusItem.menu {
                rebuildMenu(status: status, menu: menu)
                lastMenuFingerprint = fp
            }
        }
        maybeNotify(status)
    }

    private func menuFingerprint(_ status: Status?) -> String {
        guard let status else { return "nil" }
        return [
            status.enabled ? "1" : "0",
            (status.allowTailscale ?? true) ? "1" : "0",
            (status.tailscaleInstalled ?? false) ? "1" : "0",
            (status.tailscaleConnected ?? false) ? "1" : "0",
            (status.tailscaleActive ?? false) ? "1" : "0",
            status.wgConnected ? "1" : "0",
            status.unhealthy ? "1" : "0",
            status.pfOk ? "1" : "0",
            status.activeProfile ?? "-",
            status.interface ?? "-",
            status.tailscaleInterfaces?.joined(separator: ",") ?? "",
            status.icon,
            String(status.endpoints.count),
        ].joined(separator: "|")
    }

    private func updateIcon(_ status: Status?) {
        guard let button = statusItem.button else { return }
        let symbol: String
        switch status?.icon {
        case "on":
            symbol = "lock.shield.fill"
        case "error":
            symbol = "shield.slash.fill"
        case "warn":
            symbol = "exclamationmark.shield.fill"
        default:
            symbol = "shield"
        }
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "WG Kill Switch")
        button.image?.isTemplate = true

        if let status {
            var tip: String
            if status.unhealthy {
                tip = "Kill Switch ON — WireGuard down, traffic blocked"
            } else if status.enabled {
                tip = "Kill Switch ON — \(status.activeProfile ?? "WireGuard")"
            } else {
                tip = "Kill Switch OFF"
            }
            if status.tailscaleInstalled == true {
                tip += (status.allowTailscale ?? true) ? " · Tailscale OK" : " · Tailscale blocked"
            }
            button.toolTip = tip
        }
    }

    private func rebuildMenu(status: Status?, menu: NSMenu) {
        menu.removeAllItems()

        let title = NSMenuItem(title: "WG Kill Switch", action: nil, keyEquivalent: "")
        title.isEnabled = false
        menu.addItem(title)
        menu.addItem(.separator())

        if let status {
            let wgLine: String
            if status.wgConnected {
                wgLine = "WireGuard: \(status.activeProfile ?? "connected")"
            } else {
                wgLine = "WireGuard: не подключён"
            }
            menu.addItem(disabled(wgLine))

            if let iface = status.interface {
                menu.addItem(disabled("WG интерфейс: \(iface)"))
            }

            let ksLine: String
            if !status.enabled {
                ksLine = "Kill Switch: выключен"
            } else if status.unhealthy {
                ksLine = "Kill Switch: блокирует (нет WG)"
            } else {
                ksLine = "Kill Switch: включён"
            }
            menu.addItem(disabled(ksLine))

            menu.addItem(.separator())

            // Tailscale status (installation — not peer list)
            if status.tailscaleInstalled == true {
                var tsLine = "Tailscale: установлен"
                if status.tailscaleConnected == true {
                    tsLine += ", в сети"
                } else {
                    tsLine += ", не подключён"
                }
                menu.addItem(disabled(tsLine))
                if let ifaces = status.tailscaleInterfaces, !ifaces.isEmpty {
                    menu.addItem(disabled("TS интерфейс: \(ifaces.joined(separator: ", "))"))
                }
                if status.enabled {
                    let pfLine = (status.tailscaleActive == true)
                        ? "С kill-switch: Tailscale разрешён"
                        : "С kill-switch: Tailscale режется"
                    menu.addItem(disabled(pfLine))
                }
            } else {
                menu.addItem(disabled("Tailscale: не найден"))
            }

            if !status.pfOk {
                menu.addItem(disabled("PF: ошибка — см. логи"))
            }
        } else {
            menu.addItem(disabled("Демон ещё не ответил…"))
        }

        menu.addItem(.separator())

        let enabled = status?.enabled ?? false
        let toggle = NSMenuItem(
            title: enabled ? "Выключить Kill Switch" : "Включить Kill Switch",
            action: #selector(toggleKillSwitch(_:)),
            keyEquivalent: "k"
        )
        toggle.target = self
        menu.addItem(toggle)

        // Separate Tailscale coexistence toggle (checkbox)
        let tsInstalled = status?.tailscaleInstalled ?? false
        let allowTS = status?.allowTailscale ?? true
        let tsItem = NSMenuItem(
            title: "Работать с Tailscale",
            action: #selector(toggleTailscale(_:)),
            keyEquivalent: "t"
        )
        tsItem.target = self
        tsItem.state = allowTS ? .on : .off
        // Still allow toggling preference even if app not installed (for later).
        // But if not installed, show disabled checkbox with off state explanation.
        if !tsInstalled {
            tsItem.isEnabled = true
            tsItem.toolTip = "Tailscale не установлен — переключатель сохранится на будущее"
        } else {
            tsItem.toolTip = "Пускать overlay Tailscale при включённом Kill Switch"
        }
        menu.addItem(tsItem)

        menu.addItem(.separator())

        let quit = NSMenuItem(
            title: "Quit",
            action: #selector(quitApp(_:)),
            keyEquivalent: "q"
        )
        quit.target = self
        menu.addItem(quit)
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func maybeNotify(_ status: Status?) {
        guard let status else { return }
        if status.unhealthy && !lastNotifiedUnhealthy {
            postNotification(
                title: "WG Kill Switch",
                body: "WireGuard не подключён — весь трафик заблокирован"
            )
            lastNotifiedUnhealthy = true
        } else if !status.unhealthy {
            lastNotifiedUnhealthy = false
        }
    }

    private func postNotification(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let req = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(req, withCompletionHandler: nil)
    }

    @objc func toggleKillSwitch(_ sender: Any?) {
        let currently = lastStatus?.enabled ?? false
        let cmd = currently ? "disable" : "enable"
        let ok = runCtl(cmd)
        if !ok {
            postNotification(title: "WG Kill Switch", body: "Не удалось выполнить \(cmd)")
        }
        menuIsOpen = false
        lastMenuFingerprint = ""
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.refresh()
        }
    }

    @objc func toggleTailscale(_ sender: Any?) {
        let ok = runCtl("tailscale-toggle")
        if !ok {
            postNotification(title: "WG Kill Switch", body: "Не удалось переключить Tailscale")
        }
        menuIsOpen = false
        lastMenuFingerprint = ""
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.refresh()
        }
    }

    @objc func quitApp(_ sender: Any?) {
        let uid = getuid()
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        proc.arguments = ["bootout", "gui/\(uid)/com.local.wgkillswitch.agent"]
        try? proc.run()
        proc.waitUntilExit()
        NSApp.terminate(nil)
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}

extension AppDelegate: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        rebuildMenu(status: lastStatus ?? readStatus(), menu: menu)
        lastMenuFingerprint = menuFingerprint(lastStatus)
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
