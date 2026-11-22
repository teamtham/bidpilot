import Foundation
import AVFoundation
import Vision
import AppKit

let args = CommandLine.arguments
if args.count == 5 && args[1] == "--frame" {
    guard let seconds = Double(args[3]), seconds.isFinite, seconds >= 0 else { exit(2) }
    let asset = AVURLAsset(url: URL(fileURLWithPath: args[2]))
    let duration = CMTimeGetSeconds(asset.duration)
    guard duration.isFinite, duration > 0, seconds < duration, !asset.tracks(withMediaType: .video).isEmpty else { exit(3) }
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.maximumSize = CGSize(width: 640, height: 640)
    do {
        let image = try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: nil)
        let bitmap = NSBitmapImageRep(cgImage: image)
        guard let data = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.8]) else { exit(4) }
        try data.write(to: URL(fileURLWithPath: args[4]), options: .atomic)
    } catch { exit(4) }
    exit(0)
}
if args.count == 3 && args[1] == "--ocr-frames" {
    let directory = URL(fileURLWithPath: args[2], isDirectory: true)
    let pattern = try! NSRegularExpression(pattern: "^frame-[0-9]+\\.jpg$")
    let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
    let selected = names.filter {
        pattern.firstMatch(in: $0, range: NSRange(location: 0, length: $0.utf16.count)) != nil
    }.sorted().prefix(40)
    var recognized: [[String: String]] = []
    for name in selected {
        guard let image = NSImage(contentsOf: directory.appendingPathComponent(name)),
              let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
        let req = VNRecognizeTextRequest(); req.recognitionLevel = .accurate
        do {
            try VNImageRequestHandler(cgImage: cg).perform([req])
            let text = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
            recognized.append(["file": name, "text": text])
        } catch { recognized.append(["file": name, "text": ""]) }
    }
    let data = try JSONSerialization.data(withJSONObject: ["frames": recognized, "provider": "Vision"])
    FileHandle.standardOutput.write(data)
    exit(0)
}
if args.count != 3 { exit(2) }
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let duration = CMTimeGetSeconds(asset.duration)
if !duration.isFinite || duration <= 0 || duration > 180 || asset.tracks(withMediaType: .video).isEmpty { exit(3) }
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.maximumSize = CGSize(width: 640, height: 640)
generator.requestedTimeToleranceBefore = CMTime(seconds: 0.1, preferredTimescale: 600)
generator.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
func signature(_ image: CGImage) -> [UInt8] {
    var pixels = [UInt8](repeating: 0, count: 16 * 16 * 4)
    let space = CGColorSpaceCreateDeviceRGB()
    pixels.withUnsafeMutableBytes { bytes in
        if let ctx = CGContext(data: bytes.baseAddress, width: 16, height: 16, bitsPerComponent: 8, bytesPerRow: 64, space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: 16, height: 16))
        }
    }
    return pixels
}
var frames: [[String: Any]] = []
var previous: [UInt8]? = nil
var lastSaved = -10.0
let steps = min(360, max(1, Int(ceil(duration * 2))))
for i in 0..<steps {
    let sec = min(duration - 0.01, Double(i) / 2)
    do {
        var actual = CMTime.zero
        let cg = try generator.copyCGImage(at: CMTime(seconds: sec, preferredTimescale: 600), actualTime: &actual)
        let sig = signature(cg)
        var score = 0.0
        if let prev = previous {
            var sum = 0
            for j in stride(from: 0, to: sig.count, by: 4) {
                for k in 0..<3 { sum += abs(Int(sig[j+k]) - Int(prev[j+k])) }
            }
            score = Double(sum) / (Double(16*16*3) * 255.0)
        }
        previous = sig
        let cut = score > 0.26 && sec - lastSaved >= 0.5
        if frames.count >= 40 || (!cut && sec - lastSaved < 5) { continue }
        let name = "frame-\(frames.count).jpg"
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let data = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.7]) else { continue }
        try data.write(to: URL(fileURLWithPath: args[2]).appendingPathComponent(name))
        let req = VNRecognizeTextRequest(); req.recognitionLevel = .accurate
        try? VNImageRequestHandler(cgImage: cg).perform([req])
        let text = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
        frames.append(["time": CMTimeGetSeconds(actual), "file": name, "ocr": text, "cut": cut, "score": score])
        lastSaved = sec
    } catch { continue }
}
if frames.isEmpty { exit(4) }
let output: [String: Any] = ["duration": duration, "frames": frames, "provider": "AVFoundation+Vision", "version": "2", "cut_detection": "half-second frame difference heuristic; boundaries approximate"]
let data = try JSONSerialization.data(withJSONObject: output)
FileHandle.standardOutput.write(data)
