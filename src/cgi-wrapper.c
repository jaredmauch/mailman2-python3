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
const char* logident = "Mailman CGI wrapper";

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
main(int argc, char** argv, char** env __attribute__((unused)))
{
        int status;

        /* Set global command line variables */
        main_argc = argc;
        main_argv = argv;

        /* sanity check arguments */
        if (argc < 2)
                fatal(logident, MAIL_USAGE_ERROR,
                      "Usage: %s program [args...]", argv[0]);

        if (!check_command(argv[1]))
                fatal(logident, MAIL_ILLEGAL_COMMAND,
                      "Illegal command: %s", argv[1]);

        check_caller(logident, parentgroup);

        /* If we got here, everything must be OK */
        status = run_script(argv[1], argc, argv, env);
        fatal(logident, status, "%s", strerror(errno));
        return status;
}



/*
 * Local Variables:
 * c-file-style: "python"
 * End:
 */
