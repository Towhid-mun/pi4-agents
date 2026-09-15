/* Prints once per second, N times, WITHOUT an explicit fflush - the "gcc,
 * make and python detect a pipe and switch to block buffering" case
 * (P2-1, trap 4). glibc fully-buffers stdout by default
 * when it is not a tty, so run directly (no stdbuf) all output arrives in
 * one lump at exit; run through perch (which wraps with `stdbuf -oL -eL`
 * when available - ADR-5) it streams one line per second like ticker.sh.
 *
 * Compile: gcc -O0 -o slow_printer slow_printer.c
 */
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 10;
    for (int i = 1; i <= n; i++) {
        printf("tick %d\n", i);
        sleep(1);
    }
    return 0;
}
