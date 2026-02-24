import AppKit
import Foundation
import WebKit

private struct ViewerConfig: Decodable {
    let project_dir: String?
    let python_bin: String?
    let host: String
    let port: Int
}

final class GPS360ViewerApp: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var backendProcess: Process?
    private var backendLogHandle: FileHandle?
    private var reloadTimer: Timer?
    private var viewerURL = URL(string: "http://127.0.0.1:8765/")!

    func applicationDidFinishLaunching(_ notification: Notification) {
        _ = notification
        createWindow()

        if let cfg = loadConfig() {
            viewerURL = URL(string: "http://\(cfg.host):\(cfg.port)/") ?? viewerURL
            let launchError = startBackend(config: cfg)
            if let launchError {
                showStartupError(message: launchError)
                return
            }
        } else {
            showStartupError(message: "Missing or invalid app config.")
            return
        }

        startLoadingLoop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        _ = sender
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        _ = notification
        reloadTimer?.invalidate()
        reloadTimer = nil

        if let process = backendProcess, process.isRunning {
            process.terminate()
            process.waitUntilExit()
        }

        do {
            try backendLogHandle?.close()
        } catch {
            // Ignore log close failure on exit.
        }
        backendLogHandle = nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        _ = (webView, navigation)
        reloadTimer?.invalidate()
        reloadTimer = nil
    }

    private func loadConfig() -> ViewerConfig? {
        guard let url = Bundle.main.url(forResource: "gps360-config", withExtension: "json") else {
            return nil
        }
        do {
            let data = try Data(contentsOf: url)
            return try JSONDecoder().decode(ViewerConfig.self, from: data)
        } catch {
            return nil
        }
    }

    private func createWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 180, y: 120, width: 1320, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "GPS360 Viewer"
        window.center()
        window.minSize = NSSize(width: 860, height: 560)

        let webView = WKWebView(frame: window.contentView?.bounds ?? .zero)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        window.contentView = webView

        self.window = window
        self.webView = webView
        window.makeKeyAndOrderFront(nil)
    }

    private func showStartupError(message: String) {
        guard let webView else { return }
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let html = """
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8" />
            <title>GPS360 Viewer</title>
            <style>
              body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 40px; color: #1f2933; }
              .box { max-width: 720px; padding: 18px 20px; border: 1px solid #d5dbe1; border-radius: 12px; background: #fafbfc; }
              .title { font-size: 22px; margin: 0 0 8px 0; }
              .hint { color: #5b6672; margin: 0 0 14px 0; }
              .err { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; background: #fff; border: 1px solid #e4e8ed; border-radius: 8px; padding: 10px; }
            </style>
          </head>
          <body>
            <div class="box">
              <h1 class="title">GPS360 Viewer could not start backend</h1>
              <p class="hint">Install Python 3 and ensure the app bundle was built completely.</p>
              <div class="err">\(escaped)</div>
            </div>
          </body>
        </html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func startLoadingLoop() {
        guard let webView else { return }
        webView.load(URLRequest(url: viewerURL))

        var attempts = 0
        reloadTimer = Timer.scheduledTimer(withTimeInterval: 0.9, repeats: true) { [weak self] timer in
            guard let self else {
                timer.invalidate()
                return
            }
            attempts += 1
            self.webView?.load(URLRequest(url: self.viewerURL))
            if attempts >= 20 {
                timer.invalidate()
            }
        }
        RunLoop.main.add(reloadTimer!, forMode: .common)
    }

    private func resolvePythonExecutable(preferred: String?) -> String? {
        var candidates = [String]()
        if let preferred, !preferred.isEmpty {
            candidates.append(preferred)
        }
        candidates.append(contentsOf: [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ])

        let fm = FileManager.default
        for path in candidates {
            if fm.isExecutableFile(atPath: path) {
                return path
            }
        }
        return nil
    }

    private func startBackend(config: ViewerConfig) -> String? {
        guard let pythonExecutable = resolvePythonExecutable(preferred: config.python_bin) else {
            return "Python 3 executable not found. Checked /opt/homebrew/bin/python3, /usr/local/bin/python3, and /usr/bin/python3."
        }
        guard let resourcesURL = Bundle.main.resourceURL else {
            return "Bundle resources directory is unavailable."
        }

        let moduleRoot = resourcesURL.appendingPathComponent("python", isDirectory: true)
        let gpsModulePath = moduleRoot.appendingPathComponent("gps360", isDirectory: true)
        guard FileManager.default.fileExists(atPath: gpsModulePath.path) else {
            return "Bundled Python module missing at \(gpsModulePath.path). Rebuild the app bundle."
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonExecutable)
        process.arguments = [
            "-m",
            "gps360.web_app",
            "--transport",
            "usb",
            "--host",
            config.host,
            "--port",
            String(config.port),
        ]
        process.currentDirectoryURL = moduleRoot

        var env = ProcessInfo.processInfo.environment
        if let existing = env["PYTHONPATH"], !existing.isEmpty {
            env["PYTHONPATH"] = "\(moduleRoot.path):\(existing)"
        } else {
            env["PYTHONPATH"] = moduleRoot.path
        }
        let bundledLibusb = resourcesURL
            .appendingPathComponent("lib", isDirectory: true)
            .appendingPathComponent("libusb-1.0.dylib")
        if FileManager.default.fileExists(atPath: bundledLibusb.path) {
            env["GPS360_LIBUSB_PATH"] = bundledLibusb.path
        }
        process.environment = env

        let logURL = URL(fileURLWithPath: "/tmp/gps360-gui.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        do {
            let handle = try FileHandle(forWritingTo: logURL)
            try handle.seekToEnd()
            process.standardOutput = handle
            process.standardError = handle
            backendLogHandle = handle
        } catch {
            // If log setup fails, continue without a file sink.
        }

        do {
            try process.run()
            backendProcess = process
            return nil
        } catch {
            return "Backend launch failed: \(error.localizedDescription)"
        }
    }
}

let app = NSApplication.shared
let delegate = GPS360ViewerApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
