"""Internal model-provider seam.

`ModelService` remains the only facade other services use. These modules
hold provider-specific transport/detection and shared result/error types so
an alternate local backend (e.g. Colibri "Deep Local") can exist without
touching the Ollama chat path.
"""
