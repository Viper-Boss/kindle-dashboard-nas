package statistic

import (
	"sync"
	"testing"
	"time"
)

func TestShortConnectionsAndMidnight(t *testing.T) {
	l := newClientLedger()
	a := time.Date(2026, 12, 31, 23, 59, 59, 0, l.zone)
	l.add("::ffff:192.168.1.8", 123, 456, a)
	l.add("192.168.1.8", 7, 9, a.Add(time.Second))
	r := l.snapshot()
	if len(r.Clients) != 2 || r.Clients[0].Upload != 123 || r.Clients[1].Download != 9 || r.Clients[0].Client != "192.168.1.8" { t.Fatal(r) }
}

func TestConcurrentByteAccounting(t *testing.T) {
	l := newClientLedger(); var wg sync.WaitGroup
	for i := 0; i < 100; i++ { wg.Add(1); go func() { defer wg.Done(); for j := 0; j < 100; j++ { l.add("192.168.1.8", 1, 2, time.Now()) } }() }
	wg.Wait(); r := l.snapshot()
	if len(r.Clients) != 1 || r.Clients[0].Upload != 10000 || r.Clients[0].Download != 20000 { t.Fatal(r) }
}
