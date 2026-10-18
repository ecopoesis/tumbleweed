"""Voice: audio capture, speech-to-text and push-to-talk (S06), and text-to-speech with barge-in
(S07). Hands-free listening comes in S08.

The package is GTK-free. Only the GStreamer adapters (`_gst`, `gstreamer`) import `gi`; the rest is
tested with fakes (`fake`), and `VoiceInput` and `SpeechOutput` talk to the shell through observers
and a main-thread dispatcher that the shell injects.
"""
