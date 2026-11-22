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
   Skriv koden med åtte tegn eller lim inn koden i feltet **Paringskode**. Velg båtens område,
   eller hele installasjonen hvis den bare brukes om bord. Velg enkelte entiteter i lista ved
   behov. Eldre paringskoder og kopiert QR-innhold virker fortsatt.
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

Under **Konfigurer** på en paret integrasjon velger du område eller hele installasjonen én gang.
Deretter dukker nye støttede sensorer innenfor valget opp under **Legg til sensorer** i Nausto.
Et lokalt fjernet valg
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
beholder identiteten. En erstatning med ny register-ID er en ny sensor i Nausto, og vises der
automatisk når den ligger innenfor delingsvalget.
Navn brukes ikke til å gjette hva som måles. Maksimalt 64 entiteter fra 16 enheter deles per
tilkobling, med opptil fem lokale tilkoblingsprofiler.

Støttede størrelser er spenning, strøm, effekt, brukt kapasitet, temperatur, prosent, hastighet,
varighet, kumulativ energi, trykk, volum, volumstrøm og akselerasjon med dokumenterte enheter,
antall fra HAs `counter`-entiteter, binære tilstander/alarmer, enum-tilstander og hendelser fra
`event`-entiteter, som knapper, ringeklokker og bevegelse. En hendelse sendes som «på» med
tidspunktet hendelsen skjedde, og Nausto får vite at den er en hendelse. Energi fra
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
gjentatt sending og omstart. Den vanlige veien begrenses til én forespørsel per 31 sekunder, med
eksponentiell venting og respekt for serverens `Retry-After` (maksimalt én time). En ny verdi på en
av/på- eller alarmkanal kan i tillegg gå straks på alarmsporet, beskrevet nedenfor.

Profilen rommer høyst 10 000 observasjoner og 10 MiB, inkludert åpent opptak og kontrollsporet.
Målekøen beholder opptak i høyst sju dager. Det åpne målevinduet har 100 observasjoner / 64 KiB;
kontrollsporet har 32 tilstandspunkter / 64 KiB. Dermed har forseglede målebatcher høyst
9 868 observasjoner og 10 MiB minus begge 64 KiB-reservene.
Gamle eller overskytende batcher kasseres hele og telles som datagap. Etter tolv mislykkede
forsøk beholdes opptaket som avvist. **Konfigurer → Prøv tilkoblingen på nytt** åpner for
nye forsøk. Home Assistant viser et reparasjonsvarsel ved permanente avvisninger eller tap.
Last ned integrasjonens **diagnostikk** for køtellere, gap, feiltype, om sporene og
liveforbindelsen er i bruk og om livevisningen strømmer, med egne tellere. Diagnostikken har ingen
paringskode, produsentnøkkel, livebillett, entitetsnavn, verdier eller private nettadresser.

Hvis HA er avslått, integrasjonen er deaktivert eller hendelseskøen er full, kan målinger gå
tapt. Historikk rekonstrueres ikke fra HAs Recorder. Ved oppstart brukes bare en faktisk
rapport som fortsatt finnes i tilstandsmaskinen; tidsrommet uten opptak er et hull.
En full HA-sikkerhetskopi kan inneholde den private produsentnøkkelen og køen: beskytt den.
En gjenoppretting med endret HA-instans-ID krever et nytt oppsett og gjenbruker ikke gammel kø.
Fjern de gamle Nausto-integrasjonene i HA og legg til på nytt. Når siste integrasjon fjernes,
slettes de lokale profilene og nøklene; allerede mottatt historikk i Nausto beholdes.

Utgående trafikk går bare til Naustos HTTPS-mottak og til liveforbindelsen på
`wss://nausto.no/integrasjoner/sensors/live`, som bærer vekking og, mens noen ser på båten i Nausto,
en kopi av målingene. Tilkoblingsnøkkelen er bundet til én båt og gir skriveadgang for valgte
sensorer. Styring av brytere og lys står nedenfor.

## Styring av brytere og lys

Du kan styre brytere og lys du velger i lista, og dem som sitter på samme enhet som en sensor du
velger der. Det finnes ingen egen lov om bord, og eieren
aktiverer ingenting i Nausto. Innstillinger, diagnostikk og skjulte entiteter på enheten følger ikke
med av seg selv. Det er høyst 16 kretser, de valgte først, og fortsatt høyst 16 kilder totalt per
tilkobling. En ren bryter trenger ingen kunstig sensorverdi.

En krets med lensepumpe eller alarm i navnet, entitets-ID-en eller enhetens navn (`lens`, `läns`,
`læns`, `bilge`, `alarm`) kan slås på, men aldri av. Det kontrolleres både her og i Nausto, og
beskyttelsen beholdes gjennom nytt navn, alternativer, ny autentisering og omstart; appen kan ikke
senke den. Det samme gjelder kretser som
var beskyttet i 0.3.0. Kretsene 0.3.0 tillot, kan fortsatt styres og står valgt i lista til
oppsettet lagres på nytt. Fravalg gjelder også mens en nettforespørsel venter. Bare registrerte
`switch`/`light` og deres konkrete `turn_on`/`turn_off` støttes. Dimming bruker HAs 255 trinn, med
minste positive nivå 1/255.

En bryter eller et lys med ankerspill, vinsj eller propell i navnet, entitets-ID-en eller enhetens
navn (som `ankerspill`, `windlass`, `vinsj`, `winch`, `propell` og `thruster`, også i sammensatte
ord) står ikke i lista og tilbys eller styres aldri, heller ikke om 0.3.0 tillot den. Nausto vet
ikke om noen er om bord når slikt utstyr starter. Anker alene teller ikke, så ankerlyset kan styres.
Navnene leses på nytt rett før hvert kall.

Kommandoer kommer bare i allerede autoriserte HTTPS-svar, aldri over liveforbindelsen, enten den
vanlige veien med målingenes fartsgrense og `Retry-After` eller styringssporet beskrevet nedenfor.
Hvert forsøk har en ny tilfeldig forespørselsverdi som serveren må bekrefte.
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

## Rask styring og alarmoverganger

Når Nausto støtter det, har integrasjonen to prioriterte spor til det samme HTTPS-mottaket, ved
siden av den vanlige veien. Styringssporet henter en ventende kommando straks Nausto vekker
integrasjonen, og sender kvitteringen og kretsens kilderapporter i ett minutt etter en utført
kommando.
Alarmsporet sender straks vinduet med en ny verdi på en av/på- eller alarmkanal. Tall, endret
tilgjengelighet, første verdi og tilbudet av kretser følger den vanlige takten. Sporene bruker høyst
100 og 50 forespørsler i en rullerende time og venter ikke på den vanlige veiens 31 sekunder.
Avslår Nausto en forespørsel på et spor, eller er sporet brukt opp, står køen, forsøkene og den
vanlige ventetiden urørt, og meldingen går den vanlige veien. Feil ved nøkkelen stopper hele
tilkoblingen som før.

Vekkingen kommer over en utgående forbindelse til `wss://nausto.no/integrasjoner/sensors/live`.
Den bærer vekking, en ping hvert 45. sekund og livevisningen nedenfor, aldri kommandoer eller
resultater.
Billetten til forbindelsen kommer i et godtatt HTTPS-svar, varer høyst ti minutter og står i
håndtrykkets protokollfelt, aldri i adressen. Den logges, lagres og vises aldri. Med under fem
minutter igjen henter integrasjonen en ny og kobler til på nytt med den. Etter et brudd kobles den
til igjen med tilfeldig spredt venting fra ett sekund, doblet til ett minutt. Paring og kortkode
bruker aldri sporene eller forbindelsen.

Mot en eldre Nausto-server oppfører integrasjonen seg som 0.4.0: alt går den vanlige veien, og den
spør igjen etter ti minutter. Er live slått av hos Nausto, virker sporene uten vekking, og
kommandoen hentes med neste vanlige livstegn. Dette er ingen garantert alarmkanal: nett,
autorisasjon og serverens svar kan utsette levering, og den vanlige veien står alltid som reserve.

## Livevisning

Mens noen ser på båten i Nausto, sender integrasjonen også målingene over liveforbindelsen, så
verdiene vises uten å vente på minuttbatchen. Når Nausto ber om det, går først siste kjente verdi
for hver delt kanal med kildens egne tider og måle-ID, og deretter hver ny tilstand fra HA, også
når køen venter med den på grunn av dødbånd eller ett opptak i minuttet. En verdi bare
livevisningen har fått, har en egen måle-ID og står bare i minnet; kommer samme avlesning senere i
en batch, har den samme ID der. En gjentatt tilstand og en verdi eldre enn sju dager går ikke live,
og en tilstand uten kildetid, som en gjenopprettet tilstand ved oppstart, tar kanalen ut av
øyeblikksbildet til neste daterte verdi. Siste batchede opptak per kanal beholdes i den private
køfila til et nyere erstatter det eller kanalen velges bort, så øyeblikksbildet har en verdi også
etter omstart av HA.

Tall samles og går høyst én gang i sekundet med siste verdi per kanal; av/på, tilstander, ukjent og
utilgjengelig går straks. Høyst 30 meldinger går på ti sekunder, og det som må vente, slås sammen,
så siste verdi for hver kanal kommer fram. Sier Nausto stopp, eller brytes eller fornyes
forbindelsen, stopper strømmen, og det som venter, forkastes til Nausto ber om det igjen.

Livevisningen gjør aldri en gammel verdi ny og erstatter ikke batchene: historikken kommer bare
gjennom HTTPS-mottaket, og køen, sporene og fartsgrensene er de samme. En Nausto-server uten
livevisning ber aldri om verdiene.

## English

Add `https://github.com/nausto/nausto-homeassistant` as a custom **Integration** repository in
HACS, download **Nausto**, and restart Home Assistant. In Nausto, open your boat's sensors,
create a Home Assistant connection and copy the ten-minute pairing code. In HA, add the
**Nausto** integration, enter its eight-character code or paste a copied code and choose the
boat's area, or the entire installation if it is only used on board. You can also choose individual
entities. Older codes and copied QR payloads remain supported. Return to Nausto
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

For an existing pairing, use **Configure** once to choose the area or installation. New supported
sensors in that scope then appear under **Add sensors** in Nausto. HA registry IDs keep identities
stable through renames. Numeric readings use a deadband and a two-minute source-report interval, with ordinary samples
limited to once per minute. Re-reading an unchanged report never creates a fresh reading.
Actual state/alarm and quality transitions are saved immediately and wake a separate sender.
The first value remains a baseline in the minute window. Transition windows can be sent before
that minute at the next permitted batch attempt, with at least 31 seconds between regular requests.
Transitions coalesce durably while waiting. Selection checks, queued batches, network failures
and server `Retry-After` still apply; sealed retries keep identical bytes and timestamps.
Source state-change time and source report confirmation remain separate; a gateway
heartbeat never refreshes a measurement. Unknown/unavailable are preserved. A private SQLite
queue survives restarts and retries identical batches. Its seven-day / 10,000-observation /
10-MiB limits and missing HA runtime can leave gaps. Diagnostics expose counts, error types,
whether the lanes and live connection are in use and whether the live view is streaming, with its
own counters, never credentials, live tickets or sensor data.
Use **Configure** to change sharing or retry rejected data.
Unselecting a channel in Nausto also reconciles queued captures at the next selection check.
Sealed batches remain immutable; discarded captures count as gaps, while other sensors continue.

Switches and lights on the same device as a shared sensor can be turned on and off from Nausto right
away; choose other switches and lights in the same list as the sensors. There is no separate local
consent and nothing to activate in Nausto. Settings, diagnostic and hidden entities on the device do
not come along by themselves. At most 16 circuits are offered, chosen ones first. A circuit with
bilge pump or alarm in its name, entity ID or device name (`lens`, `läns`, `læns`, `bilge`, `alarm`)
can be turned on but never off, checked both here and in Nausto. That protection, and any
protection set in 0.3.0, survives renames, options, reauthentication and restarts. Circuits allowed in 0.3.0 remain
controllable and stay selected until the setup is saved again. A switch or light with windlass,
winch, propeller or thruster in its name, entity ID or device name (such as `ankerspill`,
`windlass`, `vinsj`, `winch`, `propell` or `thruster`, also inside compound words) is not listed and
is never offered or switched, even if 0.3.0 allowed it, because Nausto cannot tell whether anyone is
on board. `anchor` alone does not count, so an anchor light can still be switched. Commands arrive
only in fresh nonce-bound HTTPS responses, never over the live connection, either on the regular
path with its rate/backoff limits or on the control lane below, with a monotonic lease that
includes the full round trip and local delays. A durable attempt is required before one concrete HA service call;
restart or a lost response never repeats the physical action. Acknowledgement is separate from a
later good source event. Control reserves 64 KiB and 32 state reports within the same total profile
budget, with at most 128 attempt markers and no state history. Permanent control-message rejection
stops only control until explicit retry; unauthorized credentials stop the connection.

When the Nausto server supports it, two priority lanes to the same HTTPS endpoint run beside the
regular path. The control lane fetches a pending command as soon as Nausto wakes the integration and
sends the acknowledgement and the circuit's source reports for one minute after an executed
command. The alarm lane sends the window with a new value on an on/off or alarm channel at once.
Numbers, availability changes, first values and the circuit offer keep the regular pace. The lanes
use at most 100 and 50 requests per rolling hour and do not wait for the regular 31 seconds. A
refused or exhausted lane leaves the queue, attempts and regular wait untouched, and the message
goes the regular way; unauthorized credentials still stop the whole connection. The wake arrives
over an outbound connection to `wss://nausto.no/integrasjoner/sensors/live` that carries wake
hints, a ping every 45 seconds and the live view below, never commands or results. Its ticket
comes in an accepted HTTPS response, lasts at most ten minutes, travels in the handshake's protocol
header and never in the address, and is never logged, stored or shown. With under five minutes left
the integration fetches a new ticket and reconnects with it; after a drop it reconnects with
jittered backoff from one second, doubling to one minute. Pairing never uses the lanes or the
connection.
An older Nausto server gets the 0.4.0 behaviour; with live turned off in Nausto the lanes work
without wake hints. This is not a guaranteed alarm channel: the regular path is always the fallback.

While someone watches the boat in Nausto, the integration also sends the measurements over the live
connection, so values show without waiting for the minute batch. When Nausto asks, it first sends
the last known value of each shared channel with its own source times and measurement ID, then each
new state from HA, also when the queue holds it back because of deadbands or one capture per minute.
A value only the live view has received carries its own measurement ID and stays in memory only; if
the same reading later goes into a batch, it keeps that ID there. A repeated state and a value older
than seven days do not go live, and a state without a source time, such as a restored state at
startup, takes the channel out of the snapshot until the next dated value. The last batched capture
per channel stays in the private queue file until a newer one replaces it or the channel is
unselected, so the snapshot has a value after an HA restart too. Numbers coalesce and go at most
once a second with the latest value per channel; on/off, states, unknown and unavailable go at once.
At most 30 messages go in ten seconds, and whatever has to wait is merged so the latest value of
every channel arrives. When Nausto says stop, or the connection drops or is renewed, the stream
stops and anything waiting is discarded until Nausto asks again. The live view never makes an old
value new and never replaces the batches: history comes only through HTTPS, and the queue, lanes and
rate limits are unchanged. A Nausto server without the live view never asks for the values.

Copyright Nausto. Alle rettigheter forbeholdt. Offentlig kildekode gir ikke i seg selv en
lisens til annen bruk eller til Naustos merkevare.
