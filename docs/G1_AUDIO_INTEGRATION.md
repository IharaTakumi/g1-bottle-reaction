# G1 microphone integration

現在の `G1MicSource` は安全なplaceholderです。G1実機、Unitree SDK、公式に確認できるbuilt-in microphone取得仕様がないため、ネットワークAPIや音声形式を推測していません。

## 交換境界

```text
WindowsMicSource --replace--> G1MicSource
                              |
                              v
AudioChunk(sample_rate, waveform)
  -> normalize/resample to 16 kHz mono float32 [-1, 1]
  -> YAMNet -> MusicStateTracker -> shared ReactionEngine
```

G1側で実装するのは `AudioSource.start(consumer)` と `stop()` の境界だけです。下流へPCM waveformと正しいsource sample rateを渡せば、YAMNet、音楽状態機械、reaction、ログは変更しません。G1固有のchannel、packet、codec、clock処理をWindows sourceやYAMNet classifierへ追加しないでください。

## 実機で確認してから行うこと

1. 対象firmware/SDKに公式のmicrophone取得APIが存在するか確認する。
2. sample format、sample rate、channels、packet順序、欠損時の扱いを確認する。
3. callbackまたはnetwork readerをブロックしないchunk受け渡しを実装する。
4. 切断、timeout、終了時のresource解放を検証する。
5. 正規化contractと長時間streamingをテストする。

Unitree SDKは引き続きWindowsの依存関係へ追加しません。
