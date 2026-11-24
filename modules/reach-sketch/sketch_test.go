package main

import (
	"bytes"
	"encoding/binary"
	"hash/crc32"
	"math"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func mustSketch(t *testing.T, p int) *sketch {
	t.Helper()
	s, err := newSketch(p)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestFoldAllPrecisionsMatchesDirectIngestion(t *testing.T) {
	rng := rand.New(rand.NewSource(7126))
	hashes := []uint64{0, 1, 1 << 63, ^uint64(0)}
	for i := 0; i < 4000; i++ {
		hashes = append(hashes, rng.Uint64(), uint64(i)<<49)
	}
	var direct []*sketch
	for p := minPrecision; p <= maxPrecision; p++ {
		s := mustSketch(t, p)
		for _, h := range hashes {
			s.addHash(h)
		}
		direct = append(direct, s)
	}
	for i, fine := range direct {
		for _, coarse := range direct[:i+1] {
			got := merge(mustSketch(t, coarse.p), fine)
			if !bytes.Equal(got.registers, coarse.registers) {
				t.Fatalf("fold %d -> %d differs from direct ingestion", fine.p, coarse.p)
			}
		}
	}
}

func TestRankAndEmptyState(t *testing.T) {
	s := mustSketch(t, 4)
	if s.estimate() != 0 {
		t.Fatal("empty sketch must estimate zero")
	}
	s.addHash(0)
	s.addHash(0xA800000000000000)
	s.addHash(0xB200000000000000)
	if s.registers[0] != 61 || s.registers[10] != 1 || s.registers[11] != 3 {
		t.Fatalf("unexpected register ranks: %v", s.registers)
	}
}

func TestSparseDistinctAndLineBoundaries(t *testing.T) {
	s := mustSketch(t, 12)
	id := strings.Repeat("x", 4096)
	if err := scanIDs(strings.NewReader(id+"\r\n"+id), s); err != nil {
		t.Fatal(err)
	}
	if math.Round(s.estimate()) != 1 {
		t.Fatal("repeated maximum-length ID should estimate one")
	}
	if err := scanIDs(strings.NewReader("中文\n 中文"), s); err != nil {
		t.Fatal(err)
	}
	if math.Round(s.estimate()) != 3 {
		t.Fatal("distinct UTF-8 or leading-space IDs were lost")
	}
}

func TestErrorsAcrossSyntheticPopulations(t *testing.T) {
	rng := rand.New(rand.NewSource(194))
	const population = 20000
	const trials = 48
	var totalSquared float64
	for trial := 0; trial < trials; trial++ {
		s := mustSketch(t, 10)
		// XOR a trial-specific mask after a bijective multiplication. Distinct
		// values within a trial follow from the odd multiplier.
		offset := rng.Uint64()
		for i := 0; i < population; i++ {
			var id [8]byte
			binary.BigEndian.PutUint64(id[:], (uint64(i)*0x9e3779b97f4a7c15)^offset)
			s.add(id[:])
		}
		relative := (s.estimate() - population) / population
		totalSquared += relative * relative
	}
	rms := math.Sqrt(totalSquared / trials)
	t.Logf("48 synthetic populations, relative RMSE %.4f%%", 100*rms)
	if rms > 0.05 {
		t.Fatalf("aggregate error %.4f exceeds fixture threshold", rms)
	}
}

func TestInvalidDataNeverPublishesOrOverwrites(t *testing.T) {
	dir := t.TempDir()
	left := filepath.Join(dir, "left.rch")
	bad := filepath.Join(dir, "bad.rch")
	target := filepath.Join(dir, "combined.rch")
	original := mustSketch(t, 4).encode()
	if err := os.WriteFile(left, original, 0600); err != nil {
		t.Fatal(err)
	}
	rank := bytes.Clone(original)
	rank[5] = 62
	binary.BigEndian.PutUint32(rank[len(rank)-4:], crc32.ChecksumIEEE(rank[:len(rank)-4]))
	checksum := bytes.Clone(original)
	checksum[5]++
	for _, broken := range [][]byte{[]byte("RCH2"), original[:8], append(bytes.Clone(original), 0), rank, checksum} {
		if err := os.WriteFile(bad, broken, 0600); err != nil {
			t.Fatal(err)
		}
		if err := run([]string{"merge", target, left, bad}, nil, nil); err == nil {
			t.Fatal("corrupt sketch accepted")
		}
		if _, err := os.Stat(target); !os.IsNotExist(err) {
			t.Fatalf("invalid merge left output: %v", err)
		}
	}
	for _, ids := range []string{"good\n\n", "\xff", strings.Repeat("x", 4097)} {
		if err := run([]string{"build", "4", "-", target}, strings.NewReader(ids), nil); err == nil {
			t.Fatal("invalid input accepted")
		}
	}
	if err := run([]string{"build", "5", "-", left}, strings.NewReader("valid\n"), nil); err == nil {
		t.Fatal("replaced an existing output")
	}
	preserved, err := os.ReadFile(left)
	if err != nil || !bytes.Equal(preserved, original) {
		t.Fatalf("source changed: %v", err)
	}
}
