"""Voice: audio capture, speech-to-text and push-to-talk (S06). Text-to-speech and hands-free come
in S07 and S08.

The package is GTK-free. Only the GStreamer adapters (`_gst`, `gstreamer`) import `gi`; the rest is
tested with fakes (`fake`), and `VoiceInput` talks to the shell through an observer and a
main-thread dispatcher that the shell injects.
"""
