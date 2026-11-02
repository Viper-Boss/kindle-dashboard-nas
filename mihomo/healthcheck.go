package main

import (
    "net"
    "os"
    "time"
)

func main() {
    conn, err := net.DialTimeout("tcp", "127.0.0.1:7890", 3*time.Second)
    if err != nil {
        os.Exit(1)
    }
    conn.Close()
}
