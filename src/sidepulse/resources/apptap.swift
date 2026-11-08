// apptap: SidePulse visualizer audio helper (macOS 14.2+).
//
// Captures audio, splits it into logarithmic frequency bands with Accelerate,
// and streams band levels to stdout for `sidepulse visualize`.
//
//   apptap --list              list processes with Core Audio process objects
//   apptap --all               all system audio (global process tap)
//   apptap --mic               default microphone
//   apptap TERM [TERM ...]     processes whose bundle ID or name contains a term
//
// Stdout protocol (text, one record per line):
//   APPTAP <sample_rate> <bands>
//   F <rms> <band_0> ... <band_n-1>      ~60 times per second
// The helper exits cleanly when stdout closes or on SIGINT/SIGTERM/SIGHUP.

import Accelerate
import AudioToolbox
import AVFoundation
import CoreAudio
import Darwin
import Foundation

let bandCount = 8
let fftLog2 = 11  // 2048-point FFT
let minHz: Float = 40
let maxHz: Float = 14000
let framesPerSecond = 60.0

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("apptap: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func note(_ message: String) {
    FileHandle.standardError.write(("apptap: " + message + "\n").data(using: .utf8)!)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("\(what) failed (OSStatus \(status))") }
}

// MARK: - Core Audio property helpers

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func readObjectIDs(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> [AudioObjectID] {
    var addr = address(selector)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.stride)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func readUInt32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var addr = address(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr ? value : nil
}

func readPID(_ object: AudioObjectID) -> pid_t {
    var addr = address(kAudioProcessPropertyPID)
    var value: pid_t = 0
    var size = UInt32(MemoryLayout<pid_t>.size)
    return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr ? value : 0
}

func readString(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr,
          let string = value?.takeRetainedValue() else { return "" }
    return string as String
}

struct AudioProcess {
    let object: AudioObjectID
    let pid: pid_t
    let bundleID: String
    let name: String
    let playing: Bool
}

func processName(_ pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4096)
    guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return "" }
    return URL(fileURLWithPath: String(cString: buffer)).lastPathComponent
}

func audioProcesses() -> [AudioProcess] {
    readObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).map { object in
        let pid = readPID(object)
        return AudioProcess(object: object, pid: pid,
                            bundleID: readString(object, kAudioProcessPropertyBundleID),
                            name: processName(pid),
                            playing: (readUInt32(object, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0)
    }
}

// MARK: - Output

let stdout = FileHandle.standardOutput
var cleanupActions: [() -> Void] = []

func cleanupAndExit(_ code: Int32 = 0) -> Never {
    for action in cleanupActions.reversed() { action() }
    exit(code)
}

func emit(_ line: String) {
    do {
        try stdout.write(contentsOf: (line + "\n").data(using: .utf8)!)
    } catch {
        cleanupAndExit(0)  // reader went away
    }
}

// MARK: - Band analysis

final class BandAnalyzer {
    let size = 1 << fftLog2
    let hop: Int
    let setup: FFTSetup
    let window: [Float]
    let bandBins: [Range<Int>]
    var ring: [Float]
    var pending = 0
    var real: [Float]
    var imag: [Float]
    var magnitudes: [Float]
    var windowed: [Float]

    init(sampleRate: Double) {
        hop = max(64, Int(sampleRate / framesPerSecond))
        guard let fft = vDSP_create_fftsetup(vDSP_Length(fftLog2), FFTRadix(kFFTRadix2)) else {
            fail("vDSP_create_fftsetup failed")
        }
        setup = fft
        var hann = [Float](repeating: 0, count: size)
        vDSP_hann_window(&hann, vDSP_Length(size), Int32(vDSP_HANN_NORM))
        window = hann
        ring = [Float](repeating: 0, count: size)
        real = [Float](repeating: 0, count: size / 2)
        imag = [Float](repeating: 0, count: size / 2)
        magnitudes = [Float](repeating: 0, count: size / 2)
        windowed = [Float](repeating: 0, count: size)

        let binHz = Float(sampleRate) / Float(size)
        let top = min(maxHz, Float(sampleRate) / 2 * 0.95)
        var ranges: [Range<Int>] = []
        for band in 0..<bandCount {
            let low = minHz * powf(top / minHz, Float(band) / Float(bandCount))
            let high = minHz * powf(top / minHz, Float(band + 1) / Float(bandCount))
            let lowBin = max(1, Int((low / binHz).rounded(.down)))
            let highBin = min(size / 2, max(lowBin + 1, Int((high / binHz).rounded(.up))))
            ranges.append(lowBin..<highBin)
        }
        bandBins = ranges
    }

    func push(_ samples: UnsafeBufferPointer<Float>) {
        var offset = 0
        while offset < samples.count {
            let take = min(hop - pending, samples.count - offset)
            ring.removeFirst(take)
            ring.append(contentsOf: samples[offset..<offset + take])
            offset += take
            pending += take
            if pending >= hop {
                pending = 0
                analyze()
            }
        }
    }

    func analyze() {
        vDSP_vmul(ring, 1, window, 1, &windowed, 1, vDSP_Length(size))
        var rms: Float = 0
        vDSP_rmsqv(Array(ring[(size - hop)...]), 1, &rms, vDSP_Length(hop))
        real.withUnsafeMutableBufferPointer { realPointer in
            imag.withUnsafeMutableBufferPointer { imagPointer in
                var split = DSPSplitComplex(realp: realPointer.baseAddress!, imagp: imagPointer.baseAddress!)
                windowed.withUnsafeBufferPointer { input in
                    input.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: size / 2) {
                        vDSP_ctoz($0, 2, &split, 1, vDSP_Length(size / 2))
                    }
                }
                vDSP_fft_zrip(setup, &split, 1, vDSP_Length(fftLog2), FFTDirection(FFT_FORWARD))
                split.imagp[0] = 0  // packed Nyquist term; ignore
                vDSP_zvmags(&split, 1, &magnitudes, 1, vDSP_Length(size / 2))
            }
        }
        var fields = [String(format: "%.6f", rms)]
        let scale = 1.0 / Float(size)
        for range in bandBins {
            var mean: Float = 0
            magnitudes.withUnsafeBufferPointer { pointer in
                vDSP_meanv(pointer.baseAddress! + range.lowerBound, 1, &mean, vDSP_Length(range.count))
            }
            fields.append(String(format: "%.6f", sqrtf(mean) * scale))
        }
        emit("F " + fields.joined(separator: " "))
    }
}

func mixToMono(_ list: UnsafePointer<AudioBufferList>, into mono: inout [Float]) {
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: list))
    guard let first = buffers.first, let firstData = first.mData else { mono.removeAll(); return }
    if buffers.count == 1 {
        let channels = max(1, Int(first.mNumberChannels))
        let frames = Int(first.mDataByteSize) / (4 * channels)
        let samples = firstData.assumingMemoryBound(to: Float.self)
        mono = [Float](repeating: 0, count: frames)
        for frame in 0..<frames {
            var sum: Float = 0
            for channel in 0..<channels { sum += samples[frame * channels + channel] }
            mono[frame] = sum / Float(channels)
        }
    } else {
        let frames = Int(first.mDataByteSize) / 4
        mono = [Float](repeating: 0, count: frames)
        for buffer in buffers {
            guard let data = buffer.mData else { continue }
            let samples = data.assumingMemoryBound(to: Float.self)
            for frame in 0..<frames { mono[frame] += samples[frame] / Float(buffers.count) }
        }
    }
}

// MARK: - Sources

func startProcessTap(_ description: CATapDescription) {
    description.uuid = UUID()
    description.muteBehavior = .unmuted
    description.isPrivate = true
    description.name = "SidePulse visualizer tap"
    var tapID = AudioObjectID(kAudioObjectUnknown)
    check(AudioHardwareCreateProcessTap(description, &tapID), "AudioHardwareCreateProcessTap")
    cleanupActions.append { AudioHardwareDestroyProcessTap(tapID) }

    var format = AudioStreamBasicDescription()
    var formatAddress = address(kAudioTapPropertyFormat)
    var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    check(AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &format), "read tap format")
    guard format.mFormatID == kAudioFormatLinearPCM, format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
          format.mBitsPerChannel == 32 else {
        fail("unexpected tap format")
    }

    let outputDevice = AudioObjectID(readUInt32(AudioObjectID(kAudioObjectSystemObject),
                                                kAudioHardwarePropertyDefaultSystemOutputDevice) ?? 0)
    let outputUID = readString(outputDevice, kAudioDevicePropertyDeviceUID)
    let aggregate: [String: Any] = [
        kAudioAggregateDeviceNameKey: "SidePulse visualizer",
        kAudioAggregateDeviceUIDKey: UUID().uuidString,
        kAudioAggregateDeviceMainSubDeviceKey: outputUID,
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
        kAudioAggregateDeviceTapListKey: [[
            kAudioSubTapDriftCompensationKey: true,
            kAudioSubTapUIDKey: description.uuid.uuidString,
        ]],
    ]
    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID), "create aggregate device")
    cleanupActions.append { AudioHardwareDestroyAggregateDevice(aggregateID) }

    let analyzer = BandAnalyzer(sampleRate: format.mSampleRate)
    emit("APPTAP \(Int(format.mSampleRate)) \(bandCount)")
    var mono: [Float] = []
    var procID: AudioDeviceIOProcID?
    let queue = DispatchQueue(label: "apptap.io")
    check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, input, _, _, _ in
        mixToMono(input, into: &mono)
        mono.withUnsafeBufferPointer { analyzer.push($0) }
    }, "create IO proc")
    cleanupActions.append {
        AudioDeviceStop(aggregateID, procID)
        AudioDeviceDestroyIOProcID(aggregateID, procID!)
    }
    check(AudioDeviceStart(aggregateID, procID), "start capture")
}

var engine: AVAudioEngine?

func startMicrophone() {
    let audio = AVAudioEngine()
    let input = audio.inputNode
    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0 else { fail("no microphone input available") }
    let analyzer = BandAnalyzer(sampleRate: format.sampleRate)
    emit("APPTAP \(Int(format.sampleRate)) \(bandCount)")
    let queue = DispatchQueue(label: "apptap.mic")
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
        guard let channels = buffer.floatChannelData else { return }
        let frames = Int(buffer.frameLength)
        let count = Int(buffer.format.channelCount)
        var mono = [Float](repeating: 0, count: frames)
        for channel in 0..<count {
            for frame in 0..<frames { mono[frame] += channels[channel][frame] / Float(count) }
        }
        queue.async { mono.withUnsafeBufferPointer { analyzer.push($0) } }
    }
    do { try audio.start() } catch { fail("microphone start failed: \(error)") }
    cleanupActions.append { audio.stop() }
    engine = audio
}

// MARK: - Main

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.isEmpty || arguments.contains("-h") || arguments.contains("--help") {
    print("usage: apptap --list | --all | --mic | TERM [TERM ...]")
    exit(arguments.isEmpty ? 2 : 0)
}

signal(SIGPIPE, SIG_IGN)

if arguments == ["--list"] {
    for process in audioProcesses().filter({ $0.pid != getpid() }).sorted(by: { $0.playing && !$1.playing }) {
        print("\(process.playing ? "playing" : "-")\t\(process.pid)\t\(process.bundleID)\t\(process.name)")
    }
    exit(0)
} else if arguments == ["--all"] {
    startProcessTap(CATapDescription(stereoGlobalTapButExcludeProcesses: []))
} else if arguments == ["--mic"] {
    startMicrophone()
} else {
    let terms = arguments.map { $0.lowercased() }
    let matches = audioProcesses().filter { process in
        process.pid != getpid() && terms.contains { term in
            process.bundleID.lowercased().contains(term) || process.name.lowercased().contains(term)
        }
    }
    if matches.isEmpty {
        fail("no audio process matches \(arguments.joined(separator: ", ")). "
             + "Start playback, then run `sidepulse visualize --list-apps`.")
    }
    for match in matches { note("tapping pid \(match.pid) \(match.bundleID) \(match.name)") }
    startProcessTap(CATapDescription(stereoMixdownOfProcesses: matches.map(\.object)))
}

for signalNumber in [SIGINT, SIGTERM, SIGHUP] {
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
    source.setEventHandler { cleanupAndExit(0) }
    source.resume()
    _ = Unmanaged.passRetained(source)
}
dispatchMain()
