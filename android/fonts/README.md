# Fonts bundled in the Android app

`app/src/main/res/font/` holds two fonts, both under the SIL Open Font License
1.1 (texts in this folder). They are subsets: only the characters the app
draws, so the APK grows by under 10 KB.

| File | Font | Characters kept |
| --- | --- | --- |
| `tandem_ascii_mono.ttf` | Geist Mono, instanced at weight 700 | ` .:-=+*#%@M` (the ASCII ramp) |
| `tandem_wordmark.ttf` | Archivo Black | `Tandem` |

Why they are bundled: the splash and the connect screen's title draw a row of
characters per call, which only lines up if every character has the same
width. Phone makers substitute the system `monospace` font (AOSP has no bold
one) and may drop the `sans-serif-black` alias; on a Moto One that broke the
ASCII art. With these, every phone draws the same thing.

Rebuild them (needs `fonttools`), from Google Fonts' `ofl/geistmono` and
`ofl/archivoblack`, with layout features dropped so nothing kerns or ligates:

```python
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools import subset

def sub(font, text, path):
    opts = subset.Options(); opts.layout_features = []; opts.name_IDs = ["*"]
    s = subset.Subsetter(opts); s.populate(text=text); s.subset(font); font.save(path)

sub(instancer.instantiateVariableFont(TTFont("GeistMono[wght].ttf"), {"wght": 700}),
    " .:-=+*#%@M", "tandem_ascii_mono.ttf")
sub(TTFont("ArchivoBlack-Regular.ttf"), "Tandem", "tandem_wordmark.ttf")
```
