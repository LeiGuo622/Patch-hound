/* Synthetic PatchHound example; no upstream code or commit. */
#include <stddef.h>
#include <string.h>

int demo_store(const char *input, size_t length)
{
    char buffer[16] = {0};

    if (length > sizeof(buffer))
        return -1;
    memcpy(buffer, input, length);
    buffer[length] = '\0';
    return (unsigned char)buffer[0];
}
