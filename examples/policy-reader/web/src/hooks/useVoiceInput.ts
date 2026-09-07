/**
 * Mic publish only. This hook's entire job is: get a LiveKit token from the
 * server, join this session's voice room, turn the mic on or off.
 *
 * It does NOT decide when the listener has interrupted, does NOT transcribe
 * anything, and does NOT talk to /ws/audio. All of that already happens
 * server-side in examples/policy-reader/voice/bridge.py, which subscribes to
 * the same room from the other end and drives the existing interrupt/ask
 * flow exactly as if the listener had typed. This hook exists only so that
 * bridge has audio to listen to.
 */
import { useCallback, useRef, useState } from 'react'
import { Room, RoomEvent } from 'livekit-client'

export type VoiceInputStatus = 'idle' | 'connecting' | 'live' | 'error'

export function useVoiceInput() {
  const [status, setStatus] = useState<VoiceInputStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const roomRef = useRef<Room | null>(null)

  const stop = useCallback(async () => {
    const room = roomRef.current
    roomRef.current = null
    if (room) {
      try {
        await room.localParticipant.setMicrophoneEnabled(false)
      } finally {
        room.disconnect()
      }
    }
    setStatus('idle')
  }, [])

  const start = useCallback(async () => {
    setStatus('connecting')
    setError(null)
    try {
      const res = await fetch('/api/livekit/token')
      if (!res.ok) throw new Error(`token request failed (${res.status})`)
      const { url, token } = await res.json()
      if (!url || !token) throw new Error('voice bridge is not configured (missing LiveKit credentials)')

      const room = new Room()
      room.on(RoomEvent.Disconnected, () => {
        roomRef.current = null
        setStatus('idle')
      })
      await room.connect(url, token)
      await room.localParticipant.setMicrophoneEnabled(true)
      roomRef.current = room
      setStatus('live')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setStatus('error')
      roomRef.current = null
    }
  }, [])

  const toggle = useCallback(() => {
    if (status === 'live' || status === 'connecting') void stop()
    else void start()
  }, [status, start, stop])

  return { status, error, toggle }
}
