#include <stdio.h>

int undefined_function(int x);

int main(void) {
    printf("%d\n", undefined_function(5));
    return 0;
}
