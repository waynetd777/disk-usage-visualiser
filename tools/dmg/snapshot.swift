// Renders an HTML file to a PNG with WebKit: snapshot.swift <in.html> <out.png> <width> <height> <scale>
import AppKit
import WebKit

let a = CommandLine.arguments
let (input, output) = (URL(fileURLWithPath: a[1]), URL(fileURLWithPath: a[2]))
let (w, h, scale) = (Double(a[3])!, Double(a[4])!, Double(a[5])!)

final class Loader: NSObject, WKNavigationDelegate {
    func webView(_ web: WKWebView, didFinish _: WKNavigation!) {
        // Give web fonts a moment to be drawn.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            web.takeSnapshot(with: nil) { image, error in
                guard let image else { fatalError("snapshot: \(String(describing: error))") }
                let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(w * scale), pixelsHigh: Int(h * scale), bitsPerSample: 8,
                                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                rep.size = NSSize(width: w, height: h)
                NSGraphicsContext.saveGraphicsState()
                NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
                image.draw(in: NSRect(x: 0, y: 0, width: w, height: h))
                NSGraphicsContext.restoreGraphicsState()
                try! rep.representation(using: .png, properties: [:])!.write(to: output)
                exit(0)
            }
        }
    }
}

let app = NSApplication.shared
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: w, height: h))
let loader = Loader()
web.navigationDelegate = loader
web.loadFileURL(input, allowingReadAccessTo: input.deletingLastPathComponent())
app.run()
