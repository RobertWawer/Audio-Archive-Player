# Audio Archive Player v0.5.1

## Ustawienia

Aplikacja zapisuje ustawienia w pliku `config.json` obok `audio_player.py`. Plik jest tworzony automatycznie przy pierwszym zapisie ustawień.

Przykład:
```json
{
  "theme": "nord",
  "volume": 75,
  "last_directory": "D:\\Audio"
}
```

Zapisywane ustawienia:
- `theme` — aktywny motyw graficzny Textual;
- `volume` — głośność mpv w zakresie 0–100;
- `last_directory` — ostatnio używany katalog, otwierany przy kolejnym uruchomieniu, jeśli nie podano ścieżki w wierszu poleceń.

Naciśnij `T`, aby przełączać motywy. Ikony katalogów są monochromatyczne i przyjmują kolor akcentu aktywnego motywu. Zmiana motywu i głośności zapisuje się automatycznie. Jeśli podasz katalog przy uruchomieniu, ma on pierwszeństwo przed zapamiętanym katalogiem.

## Instalacja i uruchomienie

Instalacja zależności:
```powershell
python -m pip install -r requirements.txt
```

Uruchomienie:
```powershell
python .\audio_player.py
python .\audio_player.py .\Muzyka
python .\audio_player.py "D:\Audio\Moja kolekcja"
```

Ścieżka względna jest liczona od bieżącego katalogu PowerShella. `mpv.exe` musi być w PATH albo obok `audio_player.py`.

## Sterowanie

- `Enter` — otwórz katalog/archiwum lub odtwórz utwór
- `..` — katalog nadrzędny / wyjście z archiwum
- `Space` — pauza/wznowienie
- `S` — zatrzymanie
- `←` / `→` — przewijanie o 10 sekund
- `-` / `+` — zmiana głośności o 5 punktów
- `T` — zmiana motywu
- `Q` — wyjście
