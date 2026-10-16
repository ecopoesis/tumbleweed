"""The shell: the Adw application, its window and panels, and the transcript view-model.

Only `tumbleweed.shell.viewmodel` is GTK-free (import-linter enforces it); everything else here
is the GTK adapter. This package's `__init__` imports nothing, so that importing the view-model
never pulls GTK in through its parent package.
"""
