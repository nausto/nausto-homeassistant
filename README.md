# Nausto for Home Assistant

Del valgte sensorer med båten din i [Nausto](https://nausto.no). Integrasjonen er levert av
Nausto og er en egen integrasjon, ikke en del av Home Assistant Core.

## Installere og koble til

Home Assistant **2026.10.4 eller nyere**. Prøvd med 2026.10.4 og 2026.11.0.
Integrasjonen kjører inne i HA og fungerer med HAOS og Container. Den trenger verken MQTT,
egen tjeneste eller en Home Assistant-tilgangsnøkkel hos Nausto.

1. Åpne **HACS → menyen med tre prikker → Egendefinerte kilder**. Legg til
   `https://github.com/nausto/nausto-homeassistant`, type **Integration**. Last ned **Nausto**
   og start Home Assistant på nytt når HACS ber om det. HACS må være installert først.
   På HA med Marketplace brukes samme repository som egendefinert integrasjonskilde der
   dette tilbys. Integrasjonen er ikke oppført i den innebygde katalogen.
2. I Nausto: åpne **Båten → Sensorer → Tilkoblinger og sensorer**, opprett en tilkobling
   for **Home Assistant**, og kopier paringskoden. Du må eie båten. Koden varer ti minutter.
3. I Home Assistant: **Innstillinger → Enheter og tjenester → Legg til integrasjon → Nausto**.
   Skriv koden med åtte tegn eller lim inn koden i feltet **Paringskode**, og velg bare
   entitetene som tilhører båten. Eldre paringskoder og kopiert QR-innhold virker fortsatt.
4. Tilbake i Nausto velger du **Følg denne sensoren**, hvilken komponent den tilhører og
   eventuell primærkilde. Målinger og historikk vises når delingen er aktivert begge steder.

Du kan også installere manuelt: kopier utgivelsens `custom_components/nausto` til HA-mappen
`config/custom_components/nausto`, start HA på nytt, og følg steg 2–4. Ingen YAML er nødvendig.
Koder og nøkler skal ikke legges i chat, logger eller feilrapporter.

Åttetegnskoden bruker `https://nausto.no/integrasjoner/sensors/pair`. Målinger, eldre koder og
QR-paring beholder det eksisterende sensor-endepunktet. Kortkodeparing har fem forskjellige
kode-/nøkkelkombinasjoner per nettadresse (IPv6 /64) på ti minutter; delte nett deler grensen.
En ny kode nullstiller ikke grensen. Vent før du prøver igjen ved for mange forsøk. Identisk
retry etter tapt svar bruker ikke et nytt forsøk.

## Valg, tilstander og tid

**Konfigurer** på integrasjonen lar deg endre det lokale delingsvalget. Et lokalt fjernet valg
stopper videre deling og fjerner det fra den usendte køen. En forseglet batch omskrives ikke;
inneholder den også en fjernet kanal, kasseres hele batchen og tapet telles i diagnostikken.
Nausto beholder allerede mottatt historikk. Deaktiver eller tilbakekall tilkoblingen i Nausto
for å stoppe mottak på serveren. En ny kode for en eksisterende tilkobling brukes via HAs
vanlige forespørsel om ny autentisering. En tilbakekalt tilkobling erstattes med en ny i Nausto
og legges til som ny integrasjon i HA. En kode for en annen tilkobling kan ikke flytte køen.

Bortvalg av en kanal i Nausto stanser mottak for kanalen. HA avstemmer også usendte opptak
ved neste kontroll av kanalvalgene, uten å endre forseglede batcher. Kassert innhold telles
som datagap. De gjenværende sensorene fortsetter, også hvis valget ble endret mellom et
livstegn og en sending.

Sensorer identifiseres med HAs entitetsregister og grupperes med enhetsregisteret. Navneendring
beholder identiteten; en erstattet entitet med ny register-ID krever et nytt delingsvalg.
Navn brukes ikke til å gjette hva som måles. Maksimalt 64 entiteter fra 16 enheter deles per
tilkobling, med opptil fem lokale tilkoblingsprofiler.

Støttede størrelser er spenning, strøm, effekt, brukt kapasitet, temperatur, prosent, hastighet,
varighet, kumulativ energi, trykk, volum, volumstrøm og akselerasjon med dokumenterte enheter,
antall fra HAs `counter`-entiteter, binære tilstander/alarmer og enum-tilstander. Energi fra
`device_class: energy` konverteres til joule; `energy_storage` og energi uten oppgitt klasse
støttes ikke, siden lagret energi ikke er samme teller som brukt energi. Prosent omtolkes ikke
til liter. Antall må være et ikke-negativt heltall. Batteriprosent merkes estimert, øvrige
HA-verdier avledet: HA kan selv ha beregnet dem. Ukjent og utilgjengelig sendes som egne kvaliteter uten en
oppdiktet 0 eller av-verdi. Naustos komponentvalg og primærkilde er alltid brukerens valg.

HA `last_updated` er tidspunktet da tilstanden eller attributtene sist ble endret, ikke bevis for
når den fysiske målingen ble tatt. `last_reported` brukes som en separat kildebekreftelse når
HA faktisk har skrevet tilstanden, også uendret. Periodisk lesing og nettverkslivstegn endrer
ingen av disse tidene. Gjenopprettede tilstander deles ikke før en ny rapport. En integration
som selv republiserer sin cache, beviser ikke ny fysisk måling; vurder kildekvaliteten i HA.

## Kø og feilsøking

Opptak lagres privat i `config/.storage/nausto/`, og forsegles omtrent hvert minutt.
Numeriske verdier bruker dødbånd (for eksempel 0,1 °C eller 0,05 V), høyst ett vanlig opptak per
kanal per minutt. En faktisk ny kilderapport tas med etter to minutter uten betydelig endring.
Polling av samme rapport gir aldri nytt opptak. Faktiske tilstands-, alarm- og
kvalitetsoverganger lagres straks og vekker en egen sender uten å blokkere opptaket på HTTP.
Første verdi beholder minuttvinduet. Et vindu med overganger kan sendes før minuttet ved første
tillatte batchforsøk; overgangene samles varig mens senderen venter. Kø, delingskontroll,
nettfeil og backoff kan utsette levering. Samme forseglete bytes og måle-ID-er brukes ved
gjentatt sending og omstart. Sending begrenses til én forespørsel per 30 sekunder, med
eksponentiell venting og respekt for serverens `Retry-After` (maksimalt én time).

Profilen rommer høyst 10 000 observasjoner og 10 MiB, inkludert åpent opptak og kontrollsporet.
Målekøen beholder opptak i høyst sju dager. Det åpne målevinduet har 100 observasjoner / 64 KiB;
kontrollsporet har 32 tilstandspunkter / 64 KiB. Dermed har forseglede målebatcher høyst
9 868 observasjoner og 10 MiB minus begge 64 KiB-reservene.
Gamle eller overskytende batcher kasseres hele og telles som datagap. Etter tolv mislykkede
forsøk beholdes opptaket som avvist. **Konfigurer → Prøv tilkoblingen på nytt** åpner for
nye forsøk. Home Assistant viser et reparasjonsvarsel ved permanente avvisninger eller tap.
Last ned integrasjonens **diagnostikk** for køtellere, gap og feiltype. Diagnostikken har ingen
paringskode, produsentnøkkel, entitetsnavn, verdier eller private nettadresser.

Hvis HA er avslått, integrasjonen er deaktivert eller hendelseskøen er full, kan målinger gå
tapt. Historikk rekonstrueres ikke fra HAs Recorder. Ved oppstart brukes bare en faktisk
rapport som fortsatt finnes i tilstandsmaskinen; tidsrommet uten opptak er et hull.
En full HA-sikkerhetskopi kan inneholde den private produsentnøkkelen og køen: beskytt den.
En gjenoppretting med endret HA-instans-ID krever et nytt oppsett og gjenbruker ikke gammel kø.
Fjern de gamle Nausto-integrasjonene i HA og legg til på nytt. Når siste integrasjon fjernes,
slettes de lokale profilene og nøklene; allerede mottatt historikk i Nausto beholdes.

Utgående trafikk går bare til Naustos HTTPS-mottak. Tilkoblingsnøkkelen er bundet til én båt og
gir skriveadgang for valgte sensorer. Lokal styring krever de egne tillatelsene nedenfor.

## Styring av brytere og lys

Styring er avslått som standard, også ved oppgradering. I **Konfigurer** må den fysiske eieren
slå på **Tillat styring fra Nausto** og velge konkrete registerførte brytere og lys. Det er
høyst 16 kontroller og fortsatt høyst 16 kilder totalt per tilkobling. En ren bryter trenger
ingen kunstig sensorverdi. Deretter kreves båtens eiervalg og sikkerhetsklassifisering i Nausto.

Velg separat **Vanlige kretser som også kan slås av** bare for kretser som trygt kan slås av.
Alle andre beskytter mot avslåing. Når en krets først har fått denne lokale beskyttelsen,
beholdes den gjennom alternativer, ny autentisering og omstart; appen kan ikke senke den.
Navn brukes ikke til å gjette om en krets er kritisk. Lokal deaktivering eller fravalg gjelder
også mens en nettforespørsel venter. Bare registrerte `switch`/`light` og deres konkrete
`turn_on`/`turn_off` støttes. Dimming bruker HAs 255 trinn, med minste positive nivå 1/255.

Kommandoer kommer i allerede autoriserte HTTP-svar og deler målingenes fartsgrense og
`Retry-After`. Hvert forsøk har en ny tilfeldig forespørselsverdi som serveren må bekrefte.
Hele HTTP-rundturen og lokal venting trekkes fra en monoton levetid på høyst 60 sekunder.
Før tjenestekallet må et uforanderlig forsøk være lagret atomisk. Etter restart eller tapt svar
utføres samme kommando aldri igjen; resultatet kan sendes på nytt med identiske bytes.

Et positivt tjenestesvar er bare en kvittering. Bare en senere faktisk kildehendelse av god
kvalitet kan bekrefte tilstanden. Oppstartsbilder, periodisk lesing og `assumed_state` blir
aldri en ny bekreftet fysisk tilstand. Kontrollsporet lagrer siste ventende tilstand per
kontroll og én forseglet melding, høyst 32 tilstandspunkter totalt, uten en lokal tilstandshistorikk.
Forsøksmarkører og meldinger deler den faste 64 KiB-reserven. Høyst 128 markører beholdes;
full eller mislykket lagring hindrer nye fysiske handlinger. Bare en ny serverbekreftet klokke
kan rydde markører etter kommandoens utløp pluss fem minutter. Usendte utgåtte resultater som
ryddes, telles som datagap; forseglede retrybytes beholdes til kvittering.

Et permanent avslag på kontrollmeldinger stanser kontrollsporet med reparasjonsvarsel.
Målinger fortsetter. **Prøv tilkoblingen på nytt** åpner kontrollsporet igjen. Feil ved nøkkelen
stopper hele tilkoblingen og krever ny autentisering. Eldre backend uten kontrollstøtte får
bare de eksisterende målemeldingene.

## English

Add `https://github.com/nausto/nausto-homeassistant` as a custom **Integration** repository in
HACS, download **Nausto**, and restart Home Assistant. In Nausto, open your boat's sensors,
create a Home Assistant connection and copy the ten-minute pairing code. In HA, add the
**Nausto** integration, enter its eight-character code or paste a copied code and select the
boat entities to share. Older codes and copied QR payloads remain supported. Return to Nausto
to follow each channel, assign its component and choose its primary source. The setup and
options forms are available in English and Norwegian.

Eight-character codes use Nausto's pairing endpoint on `nausto.no`; measurements, older codes
and QR pairing keep the existing sensor endpoint. Five different code/key combinations are
allowed per network address (IPv6 /64) in ten minutes. Shared networks share this allowance.
A new code does not reset it; an identical retry after a lost response does not spend another attempt.

Supported numeric quantities include cumulative energy, pressure, volume, volume flow,
acceleration and nonnegative integer counts from HA counter entities, alongside the existing
quantities. Values use canonical SI units. Energy requires the `energy` device class;
`energy_storage` is not reinterpreted as cumulative consumption, and percentages are not volumes.

Only explicitly selected entities are offered. HA registry IDs keep identities stable through
renames. Numeric readings use a deadband and a two-minute source-report interval, with ordinary samples
limited to once per minute. Re-reading an unchanged report never creates a fresh reading.
Actual state/alarm and quality transitions are saved immediately and wake a separate sender.
The first value remains a baseline in the minute window. Transition windows can be sent before
that minute at the next permitted batch attempt, with at least 30 seconds between requests.
Transitions coalesce durably while waiting. Selection checks, queued batches, network failures
and server `Retry-After` still apply; sealed retries keep identical bytes and timestamps.
Source state-change time and source report confirmation remain separate; a gateway
heartbeat never refreshes a measurement. Unknown/unavailable are preserved. A private SQLite
queue survives restarts and retries identical batches. Its seven-day / 10,000-observation /
10-MiB limits and missing HA runtime can leave gaps. Diagnostics expose counts and error types,
never credentials or sensor data. Use **Configure** to change sharing or retry rejected data.
Unselecting a channel in Nausto also reconciles queued captures at the next selection check.
Sealed batches remain immutable; discarded captures count as gaps, while other sensors continue.

Control is off by default. Explicitly select registered switches/lights locally, and separately
identify ordinary circuits that may safely be turned off; all others remain protected. Once
local protection is set it cannot be lowered by options, reauthentication, restart or the app.
Boat-owner authorization is also required in Nausto. Commands use fresh nonce-bound HTTP
responses, the existing global rate/backoff limits and a monotonic lease that includes the full
round trip and local delays. A durable attempt is required before one concrete HA service call;
restart or a lost response never repeats the physical action. Acknowledgement is separate from
a later good source event. Control reserves 64 KiB and 32 state reports within the same total
profile budget, with at most 128 attempt markers and no state history. Permanent control-message
rejection stops only control until explicit retry; unauthorized credentials stop the connection.

Copyright Nausto. Alle rettigheter forbeholdt. Offentlig kildekode gir ikke i seg selv en
lisens til annen bruk eller til Naustos merkevare.
