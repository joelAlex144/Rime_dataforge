"""Voice-input transport (LiveKit mic capture -> VAD -> STT) for the policy
reader demo. Additive only: everything here talks to the existing
/ws/audio protocol as a client would; nothing in delivery_layer/ changes."""
