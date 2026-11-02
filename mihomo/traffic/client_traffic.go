// SPDX-License-Identifier: AGPL-3.0-only
// These counters are updated on byte transfer, so closed short connections are retained.
package statistic

import (
	"crypto/rand"
	"encoding/hex"
	"net/netip"
	"os"
	"sort"
	"sync"
	"time"
)

type ClientTrafficPoint struct {
	Day string `json:"day"`
	Client string `json:"client"`
	Upload int64 `json:"upload"`
	Download int64 `json:"download"`
}

type ClientTrafficReport struct {
	Epoch string `json:"epoch"`
	StartedAt string `json:"startedAt"`
	Clients []ClientTrafficPoint `json:"clients"`
}

type clientLedger struct {
	sync.Mutex
	epoch string
	startedAt string
	lastDay string
	rows map[string]*ClientTrafficPoint
	counts map[string]int
	zone *time.Location
}

func newClientLedger() *clientLedger {
	var id [16]byte
	_, _ = rand.Read(id[:])
	zoneName := os.Getenv("TRAFFIC_TIMEZONE")
	if zoneName == "" { zoneName = "Asia/Shanghai" }
	zone, err := time.LoadLocation(zoneName)
	if err != nil { zone = time.FixedZone("Asia/Shanghai", 8*3600) }
	return &clientLedger{epoch: hex.EncodeToString(id[:]), startedAt: time.Now().UTC().Format(time.RFC3339Nano), rows: make(map[string]*ClientTrafficPoint), counts: make(map[string]int), zone: zone}
}

var clientTraffic = newClientLedger()

func (l *clientLedger) add(client string, upload, download int64, now time.Time) {
	if upload <= 0 && download <= 0 { return }
	if upload < 0 { upload = 0 }; if download < 0 { download = 0 }
	if ip, err := netip.ParseAddr(client); err == nil { client = ip.Unmap().String() } else { client = "unknown" }
	day := now.In(l.zone).Format("2006-01-02")
	l.Lock()
	defer l.Unlock()
	if day != l.lastDay {
		cutoff := now.In(l.zone).AddDate(0, 0, -34).Format("2006-01-02")
		for key, row := range l.rows { if row.Day < cutoff { delete(l.rows, key) } }
		for d := range l.counts { if d < cutoff { delete(l.counts, d) } }
		l.lastDay = day
	}
	key := day + "|" + client
	if l.rows[key] == nil {
		if l.counts[day] >= 4096 { client = "unknown:overflow"; key = day + "|" + client }
		if l.rows[key] == nil { l.rows[key] = &ClientTrafficPoint{Day: day, Client: client}; l.counts[day]++ }
	}
	l.rows[key].Upload += upload
	l.rows[key].Download += download
}

func (l *clientLedger) snapshot() ClientTrafficReport {
	l.Lock()
	defer l.Unlock()
	rows := make([]ClientTrafficPoint, 0, len(l.rows))
	for _, row := range l.rows { rows = append(rows, *row) }
	sort.Slice(rows, func(i, j int) bool { if rows[i].Day == rows[j].Day { return rows[i].Client < rows[j].Client }; return rows[i].Day < rows[j].Day })
	return ClientTrafficReport{Epoch: l.epoch, StartedAt: l.startedAt, Clients: rows}
}

func RecordClientTraffic(client string, upload, download int64) { clientTraffic.add(client, upload, download, time.Now()) }
func ClientTrafficSnapshot() ClientTrafficReport { return clientTraffic.snapshot() }
