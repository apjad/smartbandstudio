# Songbank — regler til AI-genererede sange

Kopiér denne fil ind i en chat med ChatGPT, Grok eller en anden AI. Skriv hvilken sang (og
gerne kunstner) du vil have en akkordoversigt til. Bed AI'en generere sangen efter reglerne
herunder, og aflever kun JSON'en tilbage.

## Format

Output skal være **præcis ét JSON-objekt**, intet andet (ingen forklaring, ingen
markdown-kodeblok udenom):

```json
{
  "title": "Sangens titel",
  "artist": "Kunstner (kan være tom streng)",
  "tempoBPM": 120,
  "beatsPerBar": 4,
  "parts": [
    { "label": "Intro", "barCount": 4, "chords": "| C | G | Am | F |" },
    { "label": "Verse", "barCount": 8, "chords": "| C | G | Am | F | C | G | F | F |" },
    { "label": "Chorus", "barCount": 8, "chords": "| F | C | G | Am | F | C | G | G |" }
  ],
  "sequence": ["Intro", "Verse", "Chorus", "Verse", "Chorus", "Chorus"]
}
```

## Regler

1. **`parts`**: højst **5** navngivne dele (SmartBand Studio har 5 part-slots). Giv dem korte,
   genkendelige navne (`Intro`, `Verse`, `Chorus`, `Bridge`, `Outro` — eller `Verse 1`/`Verse 2`
   hvis de reelt har forskellige akkorder). To dele med samme akkorder er ikke to dele — det er
   én del gentaget i `sequence`.
2. **`barCount`**: antal takter i den del, 1-48. Skal stemme overens med hvor mange akkorder
   `chords`-strengen faktisk indeholder.
3. **`chords`**: lead-sheet-stil tekst, samme format som appens egen akkord-editor:
   - Takter adskilt med `|`: `| C | Am | F G |`.
   - Flere akkorder i én takt deler den ligeligt (`F G` i 4/4 = F på slag 1, G på slag 3).
   - `.` eller `-` betyder "behold forrige akkord" i den takt/det slag.
   - `%` som en hel takt gentager forrige takt.
   - Becifring: becifringsnavn som `C`, `C#`/`Db`, `Am`, `F#m7`, `Gmaj7`, `Bbdim`, `D/F#`
     (becifring med bas-tone). Undgå becifringer appen ikke kan tolke (fx `9`, `13`, `m7b5`) —
     brug den nærmeste simple becifring i stedet (fx `7` i stedet for `9`).
4. **`sequence`**: rækkefølgen sangen faktisk spilles i, som en liste af navne fra `parts`
   (samme navn kan optræde flere gange, fx `["Intro","Verse","Chorus","Verse","Chorus","Outro"]`).
   Alle navne i `sequence` skal findes i `parts`.
5. **`tempoBPM`**: realistisk tempo for sangen. **`beatsPerBar`**: normalt 4, brug 3 for en
   vals/3-delt takt.
6. Hold det **realistisk** — skriv den faktiske akkordrækkefølge for sangen, ikke en gætteriff.
   Er du usikker på et par akkorder, brug den mest almindelige/kendte version af sangen.

## Sådan bruges resultatet

Åbn admin-siden (`Start SmartBandStudio-editor.command` på Desktop), indsæt sangens titel (og
evt. "Hent med AI"-knappen gør det samme automatisk), eller indsæt AI'ens JSON direkte i
"Indsæt AI-JSON"-feltet i editoren.
