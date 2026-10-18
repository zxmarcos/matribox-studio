# Captures for the NAM example

No capture is distributed with this repository: captures belong to their authors. Put a NAM **A2** capture
(`.nam`, a `SlimmableContainer` with the 3-channel A2-Lite network, as trained by NAM 0.7+ or TONE3000) in this
folder; `examples/build_all.py` builds the demo effect from the first one it finds, or build it directly:

```sh
python examples/nam_a2/build_nam.py examples/nam_a2/models/your_capture.nam --name "My Amp"
```

`.nam` files in this folder are ignored by git.
