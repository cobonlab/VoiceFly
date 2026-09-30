// Original Voice Fly vector marks. Generates previews and installable assets.
import AppKit
import CoreGraphics
import Foundation

enum Command {
  case move(CGFloat, CGFloat), line(CGFloat, CGFloat)
  case curve(CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat)
  case close
}
struct Shape {
  let commands: [Command]
  let width: CGFloat
  let filled: Bool
  var path: CGPath {
    let p = CGMutablePath()
    for command in commands {
      switch command {
      case let .move(x, y): p.move(to: CGPoint(x: x, y: y))
      case let .line(x, y): p.addLine(to: CGPoint(x: x, y: y))
      case let .curve(a, b, c, d, x, y):
        p.addCurve(to: CGPoint(x: x, y: y), control1: CGPoint(x: a, y: b), control2: CGPoint(x: c, y: d))
      case .close: p.closeSubpath()
      }
    }
    return p
  }
  var svg: String {
    let d = commands.map { command -> String in
      switch command {
      case let .move(x, y): return "M\(x),\(y)"
      case let .line(x, y): return "L\(x),\(y)"
      case let .curve(a, b, c, d, x, y): return "C\(a),\(b) \(c),\(d) \(x),\(y)"
      case .close: return "Z"
      }
    }.joined(separator: " ")
    return "<path d=\"\(d)\" fill=\"\(filled ? "currentColor" : "none")\" stroke=\"\(filled ? "none" : "currentColor")\" stroke-width=\"\(width)\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>"
  }
}
let capsule = Shape(commands: [.move(62,23), .curve(57.029,23,53,27.029,53,32), .line(53,50), .curve(53,54.971,57.029,59,62,59), .curve(66.971,59,71,54.971,71,50), .line(71,32), .curve(71,27.029,66.971,23,62,23), .close], width: 4.5, filled: false)
let solidCapsule = Shape(commands:capsule.commands,width:0,filled:true)
let frame = Shape(commands:[.move(45,46),.line(45,50),.curve(45,59.389,52.611,67,62,67),.curve(71.389,67,79,59.389,79,50),.line(79,46),.move(62,67),.line(62,78),.move(54,78),.line(70,78)],width:4.5,filled:false)
let breeze = Shape(commands:[.move(12,37),.line(26,37),.curve(31,37,34,34,34,30),.move(18,49),.line(28,49),.curve(33,49,36,46,36,43)],width:4.5,filled:false)
let straightWind = Shape(commands:[.move(13,35),.line(37,35),.move(23,48),.line(37,48)],width:4.5,filled:false)
let marks: [[Shape]] = [[capsule,frame,breeze],[solidCapsule,frame,straightWind]]

func paint(_ context: CGContext, mark: Int, color: CGColor, rect: CGRect) {
  context.saveGState()
  context.translateBy(x: rect.minX, y: rect.minY)
  context.scaleBy(x: rect.width / 100, y: rect.height / 100)
  context.setStrokeColor(color); context.setFillColor(color)
  context.setLineCap(.round); context.setLineJoin(.round)
  for shape in marks[mark] {
    context.addPath(shape.path); context.setLineWidth(shape.width)
    context.drawPath(using: shape.filled ? .fill : .stroke)
  }
  context.restoreGState()
}
let black = CGColor(gray: 0.08, alpha: 1)
let white = CGColor(gray: 1, alpha: 1)
let paper = CGColor(red: 0.975, green: 0.969, blue: 0.951, alpha: 1)
func png(_ size: Int, height: Int? = nil, to url: URL, draw: (CGContext) -> Void) throws {
  let height = height ?? size
  let context = CGContext(data: nil, width: size, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                          space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
  context.translateBy(x: 0, y: CGFloat(height)); context.scaleBy(x: 1, y: -1)
  draw(context)
  let bitmap = NSBitmapImageRep(cgImage: context.makeImage()!)
  try bitmap.representation(using: .png, properties: [:])!.write(to: url)
}
func preview(_ context: CGContext, mark: Int) {
  context.setFillColor(white); context.fill(CGRect(x: 0, y: 0, width: 600, height: 600))
  let rect = CGRect(x: 80, y: 60, width: 440, height: 440)
  context.addPath(CGPath(roundedRect: rect, cornerWidth: 98, cornerHeight: 98, transform: nil))
  context.setFillColor(paper); context.fillPath()
  paint(context, mark: mark, color: black, rect: CGRect(x: 138, y: 118, width: 324, height: 324))
  paint(context, mark: mark, color: black, rect: CGRect(x: 170, y: 525, width: 28, height: 28))
  paint(context, mark: mark, color: black, rect: CGRect(x: 280, y: 521, width: 36, height: 36))
  context.setFillColor(black); context.fill(CGRect(x: 390, y: 511, width: 64, height: 54))
  paint(context, mark: mark, color: white, rect: CGRect(x: 406, y: 520, width: 36, height: 36))
}
func paintMenuBadge(_ context: CGContext, mark: Int) {
  context.saveGState()
  // The native input menu can tint the whole image using only alpha. Cut the
  // mark out of the badge, rather than painting opaque black over opaque white.
  // Sequential inverse clips subtract the union of strokes without XOR seams.
  let scale: CGFloat = 11.8 / 59.5
  var transform = CGAffineTransform(translationX: (22 - 71.5 * scale) / 2, y: 2.1)
    .scaledBy(x: scale, y: scale).translatedBy(x: -9.75, y: -20.75)
  for shape in marks[mark] {
    var parts: [[Command]] = []
    for command in shape.commands {
      if case .move = command { parts.append([]) }
      parts[parts.count - 1].append(command)
    }
    for commands in parts {
      let path = Shape(commands: commands, width: shape.width, filled: shape.filled).path
      let silhouette = shape.filled ? path : path.copy(strokingWithWidth: shape.width, lineCap: .round, lineJoin: .round, miterLimit: 10)
      let cutout = silhouette.copy(using: &transform)!
      context.addRect(CGRect(x: 0, y: 0, width: 22, height: 16))
      context.addPath(cutout)
      context.clip(using: .evenOdd)
    }
  }
  context.addPath(CGPath(roundedRect: CGRect(x: 0.25, y: 0.25, width: 21.5, height: 15.5), cornerWidth: 3.8, cornerHeight: 3.8, transform: nil))
  context.setFillColor(white)
  context.setStrokeColor(CGColor(gray: 1, alpha: 0.78))
  context.setLineWidth(0.5)
  context.drawPath(using: .fillStroke)
  context.restoreGState()
}
let args = CommandLine.arguments
guard args.count >= 2 else { fputs("Usage: brand_assets OUTPUT [A|B]\n", stderr); exit(1) }
let output = URL(fileURLWithPath: args[1], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for index in marks.indices {
  let letter = ["A", "B"][index]
  let svg = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\">\(marks[index].map(\.svg).joined())</svg>\n"
  try svg.write(to: output.appendingPathComponent("option-\(letter).svg"), atomically: true, encoding: .utf8)
  try png(600, to: output.appendingPathComponent("option-\(letter).png")) { context in
    preview(context, mark: index)
  }
}
try png(900, height: 450, to: output.appendingPathComponent("voicefly-logo-options.png")) { context in
  for index in marks.indices {
    context.saveGState(); context.translateBy(x: CGFloat(index * 450), y: 0); context.scaleBy(x: 0.75, y: 0.75)
    preview(context, mark: index); context.restoreGState()
  }
}
if args.count > 2 {
  guard let selected = ["A", "B"].firstIndex(of: args[2]) else { exit(1) }
  let svg = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\" color=\"#181817\">\(marks[selected].map(\.svg).joined())</svg>\n"
  try svg.write(to: output.appendingPathComponent("voicefly.svg"), atomically: true, encoding: .utf8)
  var box = CGRect(x: 0, y: 0, width: 22, height: 16)
  let context = CGContext(output.appendingPathComponent("voicefly-menu-badge-mask.pdf") as CFURL, mediaBox: &box, nil)!
  context.beginPDFPage(nil); context.translateBy(x: 0, y: 16); context.scaleBy(x: 1, y: -1)
  paintMenuBadge(context, mark: selected)
  context.endPDFPage(); context.closePDF()
  let iconset = output.appendingPathComponent("VoiceFly.iconset", isDirectory: true)
  try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
  for base in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
      let size = base * scale
      let name = "icon_\(base)x\(base)\(scale == 2 ? "@2x" : "").png"
      try png(size, to: iconset.appendingPathComponent(name)) { context in
        let s = CGFloat(size)
        let bounds = CGRect(x: 0.055 * s, y: 0.055 * s, width: 0.89 * s, height: 0.89 * s)
        context.addPath(CGPath(roundedRect: bounds, cornerWidth: 0.2 * s, cornerHeight: 0.2 * s, transform: nil))
        context.setFillColor(paper); context.fillPath()
        paint(context, mark: selected, color: black, rect: CGRect(x: 0.16 * s, y: 0.16 * s, width: 0.68 * s, height: 0.68 * s))
      }
    }
  }
  print("Generated Voice Fly mark \(args[2])")
}
