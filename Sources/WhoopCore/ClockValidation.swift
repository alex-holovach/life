import Foundation

/// Live notifications can be buffered by iOS. They can confirm a close clock,
/// but only matched, bounded GET_CLOCK round trips may authorize a correction.
public struct ClockValidation {
    public enum Action: Equatable { case none, verified, correct }
    public private(set) var verified = false
    private var liveMatches = 0
    private var pending: (sequence: UInt8, wall: Double, uptime: Double)?
    private var previousOffset: Double?
    public init() {}

    public mutating func request(sequence: UInt8, wall: Double, uptime: Double) {
        pending = (sequence, wall, uptime)
    }
    public mutating func observeLive(device: Double, received: Double) -> Action {
        guard !verified, device.isFinite, received.isFinite else { return .none }
        liveMatches = abs(device-received) <= 3 ? liveMatches + 1 : 0
        guard liveMatches >= 2 else { return .none }
        verified = true; return .verified
    }
    public mutating func response(sequence: UInt8, device: Double, wall: Double, uptime: Double) -> Action {
        guard !verified, let request = pending, sequence == request.sequence else { return .none }
        pending = nil
        let elapsed = uptime-request.uptime
        guard device.isFinite, (0...2).contains(elapsed),
              abs((wall-request.wall)-elapsed) <= 0.5 else {
            previousOffset = nil; return .none
        }
        if device >= request.wall-3, device <= wall+3 {
            verified = true; return .verified
        }
        let offset = device-(request.wall+wall)/2
        defer { previousOffset = offset }
        guard let previousOffset, abs(offset-previousOffset) <= 1 else { return .none }
        return .correct
    }
}
