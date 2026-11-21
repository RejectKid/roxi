#requires -Version 5.1

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Speech

$inputRoot = Join-Path $PSScriptRoot '../fixtures/drill-01/input'
$sampleRate = 16000
$sampleBytes = 2
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
    $sampleRate,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)
$speakers = @{
    Dispatch = @{ Voice = 'Microsoft Zira Desktop'; Rate = 0 }
    Facilities = @{ Voice = 'Microsoft David Desktop'; Rate = -1 }
    Receiving = @{ Voice = 'Microsoft Zira Desktop'; Rate = -1 }
    Yard = @{ Voice = 'Microsoft Zira Desktop'; Rate = 1 }
    Inventory = @{ Voice = 'Microsoft David Desktop'; Rate = 1 }
    Maintenance = @{ Voice = 'Microsoft David Desktop'; Rate = 0 }
}
$timestampPattern = '\d{2}:\d{2}:\d{2}\.\d{3}'
$cuePattern = "^(?<id>[A-Z]\d+)\n(?<start>$timestampPattern) --> (?<end>$timestampPattern)\n<v (?<speaker>[^>]+)>(?<text>[^<]+)</v>$"
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()

function Get-OffsetMilliseconds {
    param([string]$Timestamp)
    $time = [TimeSpan]::ParseExact($Timestamp, 'hh\:mm\:ss\.fff', [Globalization.CultureInfo]::InvariantCulture)
    return [int]$time.TotalMilliseconds
}

function Write-PcmWave {
    param([string]$Path, [byte[]]$Samples)
    $stream = [IO.File]::Create($Path)
    $writer = [IO.BinaryWriter]::new($stream)
    try {
        $ascii = [Text.Encoding]::ASCII
        $writer.Write($ascii.GetBytes('RIFF'))
        $writer.Write([uint32](36 + $Samples.Length))
        $writer.Write($ascii.GetBytes('WAVEfmt '))
        $writer.Write([uint32]16)
        $writer.Write([uint16]1)
        $writer.Write([uint16]1)
        $writer.Write([uint32]$sampleRate)
        $writer.Write([uint32]($sampleRate * $sampleBytes))
        $writer.Write([uint16]$sampleBytes)
        $writer.Write([uint16]($sampleBytes * 8))
        $writer.Write($ascii.GetBytes('data'))
        $writer.Write([uint32]$Samples.Length)
        $writer.Write($Samples)
    } finally {
        $writer.Dispose()
    }
}

function Write-Track {
    param([string]$TrackName, [int]$DurationSeconds)
    $transcriptPath = Join-Path $inputRoot "$TrackName.vtt"
    $transcript = [IO.File]::ReadAllText($transcriptPath).Replace("`r`n", "`n").Trim()
    $sections = $transcript -split '\n\n+'
    if ($sections.Count -lt 2 -or $sections[0] -ne 'WEBVTT') {
        throw "Missing WebVTT header or cues in $transcriptPath"
    }
    $bytesPerMs = [int]($sampleRate * $sampleBytes / 1000)
    $track = [byte[]]::new($DurationSeconds * 1000 * $bytesPerMs)
    $previousEnd = 0
    $seenCueIds = [Collections.Generic.HashSet[string]]::new()

    foreach ($section in ($sections | Select-Object -Skip 1)) {
        $cue = [regex]::Match($section, $cuePattern)
        if (!$cue.Success) { throw "Unsupported cue in $transcriptPath`n$section" }
        $cueId = $cue.Groups['id'].Value
        if (!$seenCueIds.Add($cueId)) { throw "Duplicate cue $cueId in $transcriptPath" }
        $startMs = Get-OffsetMilliseconds $cue.Groups['start'].Value
        $endMs = Get-OffsetMilliseconds $cue.Groups['end'].Value
        if ($startMs -lt $previousEnd -or $endMs -le $startMs -or $endMs -gt ($DurationSeconds * 1000)) {
            throw "Invalid interval for cue $cueId in $transcriptPath"
        }
        $previousEnd = $endMs

        $speakerName = $cue.Groups['speaker'].Value
        $speaker = $speakers[$speakerName]
        if (!$speaker) { throw "Unknown speaker $speakerName in cue $cueId" }
        $synth.SelectVoice($speaker.Voice)
        $synth.Rate = $speaker.Rate
        $spokenText = $cue.Groups['text'].Value -creplace '\bOOS\b', 'O. O. S.' -creplace '\b([A-Z])-(\d+)\b', '$1 $2'
        $clip = [IO.MemoryStream]::new()
        try {
            $synth.SetOutputToAudioStream($clip, $format)
            $synth.Speak($spokenText)
            $synth.SetOutputToNull()
            $samples = $clip.ToArray()
        } finally {
            $clip.Dispose()
        }
        if (!$samples.Length -or $samples.Length -gt (($endMs - $startMs) * $bytesPerMs)) {
            throw "Speech for cue $cueId does not fit its WebVTT interval."
        }
        [Buffer]::BlockCopy($samples, 0, $track, ($startMs * $bytesPerMs), $samples.Length)
    }

    $wavePath = Join-Path $inputRoot "$TrackName.wav"
    Write-PcmWave -Path $wavePath -Samples $track
    Write-Output "$TrackName.wav | $DurationSeconds seconds | $($seenCueIds.Count) cues"
}

try {
    Write-Track -TrackName 'facilities-radio' -DurationSeconds 900
    Write-Track -TrackName 'unaligned-radio' -DurationSeconds 45
} finally {
    $synth.Dispose()
}
