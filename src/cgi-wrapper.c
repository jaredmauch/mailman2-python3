/* cgi-wrapper.c --- Generic wrapper that will take info from a environment
 * variable, and pass it to two commands.
 *
 * Copyright (C) 1998-2018 by the Free Software Foundation, Inc.
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
 *
 */

#include "common.h"

/* passed in by configure */
#define SCRIPTNAME  SCRIPT
#define LOG_IDENT   "Mailman cgi-wrapper (" SCRIPT ")"

/* Group name that your web server runs as.  See your web server's
 * documentation for details.
 */
#define LEGAL_PARENT_GROUP CGI_GROUP

const char* parentgroup = LEGAL_PARENT_GROUP;
const char* logident = LOG_IDENT;
char* script = SCRIPTNAME;

/* List of valid CGI scripts */
const char *VALID_SCRIPTS[] = {
        "admindb",
        "admin",
        "confirm",
        "create",
        "edithtml",
        "listinfo",
        "options",
        "private",
        "rmlist",
        "roster",
        "subscribe",
        NULL                                 /* Sentinel, don't remove */
};

/* Check if a script name is valid */
int check_command(char *script)
{
        int i = 0;
        while (VALID_SCRIPTS[i] != NULL) {
                if (!strcmp(script, VALID_SCRIPTS[i]))
                        return 1;
                i++;
        }
        return 0;
}

int
main(int argc __attribute__((unused)), char** argv __attribute__((unused)), char** env)
{
        int status;
        char* fake_argv[3];

        running_as_cgi = 1;

        /* Set global command line variables for --test support */
        main_argc = argc;
        main_argv = argv;

        check_caller(logident, parentgroup);

        /* For these CGI programs, we can ignore argc and argv since they
         * don't contain anything useful. `script' will always be the driver
         * program and argv will always just contain the name of the real
         * script for the driver to import and execute (padded with two dummy
         * values in argv[0] and argv[1] that are ignored by run_script()).
         */
        fake_argv[0] = NULL;
        fake_argv[1] = NULL;
        fake_argv[2] = script;

        status = run_script("driver", 3, fake_argv, env);
        fatal(logident, status, "%s", strerror(errno));
        return status;
}



/*
 * Local Variables:
 * c-file-style: "python"
 * End:
 */
