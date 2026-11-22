package main

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"math"
	"math/bits"
)

const (
	minPrecision = 4
	maxPrecision = 18
	magic        = "RCH1"
	overhead     = 9 // magic, precision byte, and four checksum bytes
)

type sketch struct {
	p         int
	registers []byte
}

func newSketch(p int) (*sketch, error) {
	if p < minPrecision || p > maxPrecision {
		return nil, fmt.Errorf("precision must be between %d and %d", minPrecision, maxPrecision)
	}
	return &sketch{p, make([]byte, 1<<p)}, nil
}

func (s *sketch) add(id []byte) {
	digest := sha256.Sum256(id)
	s.addHash(binary.BigEndian.Uint64(digest[:8]))
}

func (s *sketch) addHash(hash uint64) {
	index := hash >> (64 - s.p)
	rank := byte(min(bits.LeadingZeros64(hash<<s.p)+1, 65-s.p))
	s.registers[index] = max(s.registers[index], rank)
}

// Fold the finer sketch into the coarser one. Discarded bucket-index bits
// become a prefix of the rank suffix at the lower precision.
func merge(a, b *sketch) *sketch {
	if a.p > b.p {
		a, b = b, a
	}
	extra := b.p - a.p
	mask := (1 << extra) - 1
	for index, rank := range b.registers {
		if rank == 0 {
			continue
		}
		prefix := index & mask
		if prefix == 0 {
			rank += byte(extra)
		} else {
			rank = byte(extra - bits.Len(uint(prefix)) + 1)
		}
		coarse := index >> extra
		a.registers[coarse] = max(a.registers[coarse], rank)
	}
	return a
}

func (s *sketch) estimate() float64 {
	m := float64(len(s.registers))
	zeros := 0
	sum := 0.0
	for _, rank := range s.registers {
		sum += math.Ldexp(1, -int(rank))
		if rank == 0 {
			zeros++
		}
	}
	alpha := 0.7213 / (1 + 1.079/m)
	switch s.p {
	case 4:
		alpha = 0.673
	case 5:
		alpha = 0.697
	case 6:
		alpha = 0.709
	}
	raw := alpha * m * m / sum
	if raw <= 2.5*m && zeros != 0 {
		return m * math.Log(m/float64(zeros))
	}
	return raw
}

func (s *sketch) encode() []byte {
	out := make([]byte, overhead+len(s.registers))
	copy(out, magic)
	out[4] = byte(s.p)
	copy(out[5:], s.registers)
	end := len(out) - 4
	binary.BigEndian.PutUint32(out[end:], crc32.ChecksumIEEE(out[:end]))
	return out
}

func decode(data []byte) (*sketch, error) {
	if len(data) < overhead || string(data[:4]) != magic {
		return nil, errors.New("unrecognized sketch header")
	}
	p := int(data[4])
	if p < minPrecision || p > maxPrecision || len(data) != overhead+(1<<p) {
		return nil, errors.New("invalid precision or sketch length")
	}
	end := len(data) - 4
	if binary.BigEndian.Uint32(data[end:]) != crc32.ChecksumIEEE(data[:end]) {
		return nil, errors.New("sketch checksum mismatch")
	}
	regs := data[5:end]
	for _, rank := range regs {
		if int(rank) > 65-p {
			return nil, errors.New("register rank exceeds hash width")
		}
	}
	return &sketch{p, regs}, nil
}
