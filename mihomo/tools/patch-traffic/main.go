// SPDX-License-Identifier: AGPL-3.0-only
package main

import (
	"fmt"
	"os"
	"strings"
)

func main() {
	name := "tunnel/statistic/tracker.go"
	data, err := os.ReadFile(name); if err != nil { panic(err) }
	s := string(data)
	for _, pair := range [][2]string{
		{"tt.manager.PushUploaded(upload)", "tt.manager.PushUploaded(upload)\n\t\tRecordClientTraffic(tt.Metadata.SrcIP.String(), upload, 0)"},
		{"tt.manager.PushDownloaded(download)", "tt.manager.PushDownloaded(download)\n\t\tRecordClientTraffic(tt.Metadata.SrcIP.String(), 0, download)"},
		{"ut.manager.PushUploaded(upload)", "ut.manager.PushUploaded(upload)\n\t\tRecordClientTraffic(ut.Metadata.SrcIP.String(), upload, 0)"},
		{"ut.manager.PushDownloaded(download)", "ut.manager.PushDownloaded(download)\n\t\tRecordClientTraffic(ut.Metadata.SrcIP.String(), 0, download)"},
		{"manager.PushUploaded(uploadTotal)", "manager.PushUploaded(uploadTotal)\n\t\t\tRecordClientTraffic(metadata.SrcIP.String(), uploadTotal, 0)"},
		{"manager.PushDownloaded(downloadTotal)", "manager.PushDownloaded(downloadTotal)\n\t\t\tRecordClientTraffic(metadata.SrcIP.String(), 0, downloadTotal)"},
	} {
		if !strings.Contains(s, pair[0]) { panic("upstream traffic hook changed: " + pair[0]) }
		s = strings.ReplaceAll(s, pair[0], pair[1])
	}
	if err := os.WriteFile(name, []byte(s), 0644); err != nil { panic(err) }
	name = "hub/route/connections.go"
	data, err = os.ReadFile(name); if err != nil { panic(err) }
	s = string(data)
	anchor := "r.Get(\"/\", getConnections)"
	if strings.Count(s, anchor) != 1 { panic("upstream controller route changed") }
	s = strings.Replace(s, anchor, anchor + "\n\tr.Get(\"/clients\", func(w http.ResponseWriter, r *http.Request) { render.JSON(w, r, statistic.ClientTrafficSnapshot()) })", 1)
	if err := os.WriteFile(name, []byte(s), 0644); err != nil { panic(err) }
	fmt.Println("Applied client byte accounting and authenticated controller endpoint")
}
